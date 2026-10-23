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


def test_gains_are_hot_spot_paths_capped_at_excess_and_skip_startup():
    t = Thresholds(causes_skip_start_s=1.0, causes_ignore=("*Wait*",))
    ms = [7.0] * 300
    ms[50] = 30.0   # startup, at 0.5 s
    ms[200] = 15.0  # gameplay: 3.9 ms over budget
    frames = [Frame(i, i * 10.0, m, game_ms=m - 1, render_ms=2, rhi_ms=1, gpu_ms=2) for i, m in enumerate(ms)]
    windows = [FrameWindow(i * 0.01, i * 0.01 + 0.01) for i in range(300)]
    analysis = analyze(frames, t)

    over_read, baseline = select_frames(frames, analysis, t)
    per_frame = {i: {("game", ("Tick", "Work")): [1.0, 1.0]} for i in baseline}
    per_frame[200] = {("game", ("Tick", "Work")): [9.0, 9.0]}
    per_frame[50] = {("game", ("Tick", "Load")): [25.0, 25.0]}
    report = compute_report(frames, analysis, per_frame, baseline, over_read, windows, resolver(), t)

    assert report.non_gameplay_frames_over == 1 and report.frames_over == 1
    gains = {tuple(g.path): g.credit_ms for g in report.gains}
    assert gains[("Tick", "Work")] == pytest.approx(15.0 - 11.1)  # rise of 8 ms capped at the excess
    assert ("Tick", "Load") not in gains
