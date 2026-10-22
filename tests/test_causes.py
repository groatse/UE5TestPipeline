import csv
from pathlib import Path

import pytest

from ueperf.analyze import analyze
from ueperf.causes import compute_report, frame_rises, select_frames
from ueperf.config import Thresholds, load_thresholds
from ueperf.ingest.frames import Frame, load_frames
from ueperf.ingest.trace import Event, FrameWindow, align_frames, exclusive_ms
from ueperf.insights import InsightsError

FIXTURES = Path(__file__).parent / "fixtures"


def load_ticks(path: Path) -> list[FrameWindow]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Depth"] == "0"]
    rows.sort(key=lambda r: float(r["StartTime"]))
    return [FrameWindow(float(r["StartTime"]), float(r["EndTime"])) for r in rows]


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


def test_exclusive_time_subtracts_children_and_clips():
    windows = [FrameWindow(1.0, 1.010), FrameWindow(1.010, 1.020)]
    events = [
        Event("GameThread", "Tick", 0.995, 1.010, 0),      # 10 ms inside the game window
        Event("GameThread", "Spawn", 1.001, 1.005, 1),     # 4 ms child
        Event("RenderThread 0", "Draw", 1.012, 1.018, 0),  # render counts the following tick too
    ]
    ex = exclusive_ms(events, windows, 0)
    assert ex[("game", "Tick")] == pytest.approx(6.0)
    assert ex[("game", "Spawn")] == pytest.approx(4.0)
    assert ex[("render", "Draw")] == pytest.approx(6.0)


def test_worker_causes_count_only_when_bound_thread_waits():
    t = load_thresholds()
    frame = Frame(0, 0, 20.0, game_ms=2, render_ms=18, rhi_ms=2, gpu_ms=7)
    medians = {("render", "WaitForTasks"): 2.0, ("workers", "Decode"): 0.0}

    waiting = {("render", "WaitForTasks"): 8.0, ("workers", "Decode"): 9.0}
    causes, waits = frame_rises(frame, waiting, medians, t)
    assert [(c.thread, c.timer, c.rise_ms) for c in causes] == [("workers", "Decode", 6.0)]
    assert waits[0].timer == "WaitForTasks"

    not_waiting = {("render", "WaitForTasks"): 2.0, ("workers", "Decode"): 9.0}
    causes, _ = frame_rises(frame, not_waiting, medians, t)
    assert causes == []


def test_gains_credit_is_capped_at_excess_and_skips_startup():
    t = Thresholds(causes_skip_start_s=1.0, causes_ignore=("*Wait*",))
    ms = [7.0] * 300
    ms[50] = 30.0   # startup, at 0.5 s
    ms[200] = 15.0  # gameplay: 3.9 ms over budget
    frames = [Frame(i, i * 10.0, m, game_ms=m - 1, render_ms=2, rhi_ms=1, gpu_ms=2) for i, m in enumerate(ms)]
    windows = [FrameWindow(i * 0.01, i * 0.01 + 0.01) for i in range(300)]
    analysis = analyze(frames, t)

    over_read, baseline = select_frames(frames, analysis, t)
    per_frame = {i: {("game", "Work"): 1.0} for i in baseline}
    per_frame[200] = {("game", "Work"): 9.0, ("game", "Small"): 0.5}
    per_frame[50] = {("game", "Load"): 25.0}
    report = compute_report(frames, analysis, per_frame, baseline, over_read, windows, t)

    assert report.startup_frames_over == 1
    assert report.frames_over == 1
    gains = {g.timer: g.credit_ms for g in report.gains}
    assert gains["Work"] == pytest.approx(15.0 - 11.1)  # rise of 8 ms capped at the excess
    assert gains["Small"] == pytest.approx(0.5)
    assert "Load" not in gains
