import csv
from pathlib import Path

import pytest

from ueperf.analyze import analyze
from ueperf.causes import ScopeResolver, compute_report, explain, select_frames
from ueperf.config import Thresholds, load_thresholds
from ueperf.ingest.frames import Frame, load_frames
from ueperf.ingest.trace import Event, FrameWindow, align_frames, path_times
from ueperf.insights import InsightsError
from ueperf.source_index import SourceIndex, Site

FIXTURES = Path(__file__).parent / "fixtures"


def load_ticks(path: Path) -> list[FrameWindow]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Depth"] == "0"]
    rows.sort(key=lambda r: float(r["StartTime"]))
    return [FrameWindow(float(r["StartTime"]), float(r["EndTime"])) for r in rows]


def resolver(index=None, assets=None) -> ScopeResolver:
    return ScopeResolver(index or SourceIndex({}, {}), {}, [], assets or {})


def test_align_real_capture():
    frames = load_frames(FIXTURES / "spike2" / "frames.csv")
    windows = align_frames(frames, load_ticks(FIXTURES / "spike2" / "ticks.csv"))
    hitch = next(i for i, f in enumerate(frames) if f.frame == 1282)
    w = windows[hitch]
    # The 29.2 ms hitch lines up with the 29.1 ms tick at 13.752 s in Insights.
    assert w.start_s == pytest.approx(13.752, abs=0.001)
    assert (w.end_s - w.start_s) * 1000 == pytest.approx(frames[hitch].frame_ms, abs=0.5)


def test_align_rejects_unrelated_ticks():
    frames = [Frame(i, i * 10.0, 10.0 + (i % 7), 1, 1, 1, 1) for i in range(300)]
    ticks = [FrameWindow(i * 0.02, i * 0.02 + 0.02) for i in range(300)]
    with pytest.raises(InsightsError):
        align_frames(frames, ticks)


def test_path_times_inclusive_exclusive_and_clipping():
    windows = [FrameWindow(1.0, 1.010), FrameWindow(1.010, 1.020)]
    events = [
        Event("GameThread", "Tick", 0.995, 1.010, 0),      # 10 ms inside the game window
        Event("GameThread", "Spawn", 1.001, 1.005, 1),     # 4 ms child
        Event("RenderThread 0", "Draw", 1.012, 1.018, 0),  # render counts the following tick too
    ]
    p = path_times(events, windows, 0)
    assert p[("game", ("Tick",))] == pytest.approx([10.0, 6.0])
    assert p[("game", ("Tick", "Spawn"))] == pytest.approx([4.0, 4.0])
    assert p[("render", ("Draw",))] == pytest.approx([6.0, 6.0])


def test_explain_follows_blueprint_chain_to_hot_spot():
    t = load_thresholds()
    root = ("FEngineLoop::Tick",)
    gm = root + ("BP_GameMode_C",)
    fn = gm + ("OnTick",)
    graph = fn + ("ExecuteUbergraph_W_HUD",)
    leaf = graph + ("Widget::PlayAnimation",)
    paths = {
        ("game", root): [20.0, 1.0],
        ("game", gm): [19.0, 1.0],
        ("game", fn): [18.0, 0.0],
        ("game", graph): [18.0, 0.0],
        ("game", leaf): [18.0, 18.0],
    }
    medians = {("game", root): (2.0, 1.0), ("game", gm): (1.0, 1.0)}
    index = SourceIndex({"Widget::PlayAnimation": [Site("UserWidget.cpp", 844, "UUserWidget::PlayAnimation")]}, {})
    r = resolver(index, {"W_HUD": "Content/UI/W_HUD.uasset", "BP_GameMode": "Content/BP_GameMode.uasset"})
    expl = explain(paths, medians, 1, r, t)

    top = expl.hotspots[0]
    assert top.path == list(leaf) and top.rise_ms == 18.0
    assert top.scope.source == "UserWidget.cpp:844 in UUserWidget::PlayAnimation"
    names = [n.path[-1] for n in expl.tree]
    assert names == ["FEngineLoop::Tick", "BP_GameMode_C", "OnTick", "ExecuteUbergraph_W_HUD", "Widget::PlayAnimation"]
    kinds = {n.path[-1]: n.scope for n in expl.tree}
    assert kinds["BP_GameMode_C"].kind == "object" and "Content/BP_GameMode.uasset" in kinds["BP_GameMode_C"].source
    # OnTick calls into W_HUD's event graph, so W_HUD owns it.
    assert kinds["OnTick"].kind == "blueprint" and "in W_HUD" in kinds["OnTick"].source


def test_callers_drop_framework_scopes_and_keep_the_systems():
    t = load_thresholds()
    path = ("FEngineLoop::Tick", "Frame", "FrameTime", "Slate::Tick (Time and Widgets)", "Total Slate Tick Time",
            "PreTickEvent", "Runner Flush", "Spawn Phase")
    paths = {("game", path): [12.0, 12.0]}
    expl = explain(paths, {}, 1, resolver(), t)
    assert expl.hotspots[0].callers == ["PreTickEvent", "Runner Flush"]


def test_game_thread_cause_found_when_render_thread_only_waited():
    # The frame log blamed the render thread, but it only waited; the work was on the game thread.
    t = load_thresholds()
    spawn = ("FEngineLoop::Tick", "Runner Flush", "Spawn Phase")
    stall = ("BeginFrame", "CPU Stall - Wait For Event")
    paths = {("game", spawn): [12.7, 12.7], ("render", stall): [5.0, 5.0]}
    medians = {("render", stall): (1.0, 1.0)}
    expl = explain(paths, medians, 1, resolver(), t)
    assert expl.trace_bound == "game"
    assert expl.hotspots[0].path == list(spawn) and expl.hotspots[0].rise_ms == 12.7
    assert expl.waits[0].thread == "render"


def test_workers_count_only_when_bound_thread_waits_and_never_more_than_the_wait():
    t = load_thresholds()
    wait = ("Render", "WaitForTasks")
    work = ("Task", "Decode")
    medians = {("render", wait): (2.0, 2.0)}

    waiting = {("render", wait): [8.0, 8.0], ("workers", work): [20.0, 20.0]}
    expl = explain(waiting, medians, 1, resolver(), t)
    assert [(h.thread, h.path[-1], h.rise_ms) for h in expl.hotspots] == [("workers", "Decode", 6.0)]
    assert expl.waits[0].timer == "WaitForTasks"

    not_waiting = {("render", wait): [2.0, 2.0], ("workers", work): [20.0, 20.0]}
    assert explain(not_waiting, medians, 1, resolver(), t).hotspots == []




def test_deferred_system_names_its_work_item_and_requester():
    # H3 shape: Spawn Phase runs in Slate's pre-tick for an animation a widget asked for earlier.
    t = load_thresholds()
    loop = ("FEngineLoop::Tick", "Frame", "FrameTime", "Slate::Tick (Time and Widgets)", "Total Slate Tick Time", "PreTickEvent")
    spawn = loop + ("Runner Flush", "Spawn Phase")
    item = loop + ("Runner Flush", "Sequence Instance Update", "CounterFadeIn_INST")
    req = ("FEngineLoop::Tick", "FTimerManager_Tick", "BP_GameModeLoadedDice_C", "Client_OnCountdownTick",
           "ExecuteUbergraph_W_GameHUD", "PlayAnimationForward", "Widget::PlayAnimation")
    paths = {("game", p[:n]): [12.0, 0.0] for p in (spawn,) for n in range(1, len(p))}
    paths[("game", spawn)] = [12.0, 12.0]
    paths[("game", item[:-1])] = [2.0, 0.0]
    paths[("game", item)] = [2.0, 2.0]
    for n in range(1, len(req)):
        paths.setdefault(("game", req[:n]), [2.6, 0.0])
    paths[("game", req)] = [2.6, 2.6]
    r = resolver(assets={"W_GameHUD": "Content/UI/W_GameHUD.uasset"})
    expl = explain(paths, {}, 1, r, t)

    top = expl.hotspots[0]
    assert top.path[-1] == "Spawn Phase"
    how = {(tr.how, tr.name) for tr in top.triggered_by}
    assert ("working on", "CounterFadeIn_INST") in how
    assert ("requested by", "PlayAnimationForward") in how
    requester = next(tr for tr in top.triggered_by if tr.how == "requested by")
    assert "W_GameHUD" in requester.scope.source

    # A hot spot inside a Blueprint's own call path names that Blueprint directly.
    play = next(h for h in expl.hotspots if h.path[-1] == "Widget::PlayAnimation")
    assert play.triggered_by[0].how == "called by" and play.triggered_by[0].name == "PlayAnimationForward"
