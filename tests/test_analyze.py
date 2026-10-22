from pathlib import Path

from ueperf.analyze import MIXED, analyze, bound_type, find_hitches, find_overruns, percentile
from ueperf.config import Thresholds, load_thresholds
from ueperf.ingest.frames import Frame, load_frames

FIXTURES = Path(__file__).parent / "fixtures"


def make_frames(frame_ms: list[float], game=1.0, render=1.0, rhi=1.0, gpu=1.0) -> list[Frame]:
    return [
        Frame(frame=i, t_ms=i * 10.0, frame_ms=ms, game_ms=game, render_ms=render, rhi_ms=rhi, gpu_ms=gpu)
        for i, ms in enumerate(frame_ms)
    ]


def test_load_frames_pairs_threads_with_next_rows_duration(tmp_path):
    csv = tmp_path / "frames.csv"
    csv.write_text(
        "frame,t_ms,frame_ms,game_ms,render_ms,rhi_ms,gpu_ms\n"
        "0,0,5,40,1,1,1\n"
        "1,10,41,2,1,1,1\n"
        "2,20,6,2,1,1,1\n"
    )
    frames = load_frames(csv)
    assert [f.frame_ms for f in frames] == [41.0, 6.0]
    assert frames[0].game_ms == 40.0


def test_bound_type_and_mixed():
    t = Thresholds()
    assert bound_type(Frame(0, 0, 20, game_ms=15, render_ms=5, rhi_ms=2, gpu_ms=8), t.mixed_within) == "game"
    assert bound_type(Frame(0, 0, 20, game_ms=15, render_ms=14.5, rhi_ms=2, gpu_ms=8), t.mixed_within) == MIXED


def test_percentile_nearest_rank():
    values = sorted(float(v) for v in range(1, 101))
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0


def test_sustained_overrun_tolerates_short_gaps():
    t = Thresholds(overrun_min_frames=10, overrun_max_gap_frames=2)
    ms = [8.0] * 20 + [15.0, 15.0, 8.0, 8.0, 15.0] * 3 + [8.0] * 20
    overruns = find_overruns(make_frames(ms, gpu=12.0), t)
    assert len(overruns) == 1
    o = overruns[0]
    assert o.start_frame == 20 and o.end_frame == 34
    assert o.frames_over == 9
    assert o.bound == "gpu"


def test_gap_longer_than_allowed_splits_and_drops_short_runs():
    t = Thresholds(overrun_min_frames=10, overrun_max_gap_frames=2)
    ms = [8.0] * 20 + [15.0] * 6 + [8.0] * 3 + [15.0] * 6 + [8.0] * 20
    assert find_overruns(make_frames(ms), t) == []


def test_hitch_found_against_rolling_median():
    t = Thresholds()
    ms = [7.0] * 100 + [30.0, 25.0] + [7.0] * 50
    hitches = find_hitches(make_frames(ms), t)
    assert len(hitches) == 1
    h = hitches[0]
    assert (h.start_frame, h.end_frame, h.frames) == (100, 101, 2)
    assert h.baseline_ms == 7.0
    assert h.stall_ms == 41.0


def test_long_spike_is_not_a_hitch():
    t = Thresholds()
    ms = [7.0] * 100 + [30.0] * 4 + [7.0] * 50
    assert find_hitches(make_frames(ms), t) == []


def test_relative_spike_under_budget_is_not_a_hitch():
    t = Thresholds()
    ms = [3.0] * 100 + [9.0] + [3.0] * 50
    assert find_hitches(make_frames(ms), t) == []


def test_budget_override_from_command_line():
    assert load_thresholds(budget_ms=16.6).budget_ms == 16.6
    assert load_thresholds().budget_ms == 11.1


def test_spike_fixture():
    frames = load_frames(FIXTURES / "spike2" / "frames.csv")
    result = analyze(frames, load_thresholds())
    s = result.summary
    assert s.frames == 3827
    assert 6.0 < s.median_ms < 9.0
    assert s.frames_over == 12
    assert result.overruns == []
    assert len(result.hitches) >= 1
