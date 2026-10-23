import json
from pathlib import Path

import pytest

from ueperf.analyze import analyze
from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.markers import Marker, load_markers, markers_from_trace_times
from ueperf.ingest.trace import FrameWindow
from ueperf.phases import assign_phases
from ueperf.profile import load_profile

ROOT = Path(__file__).parent.parent


def frames_at(n: int, ms: float = 7.0) -> list[Frame]:
    return [Frame(i, i * 10.0, ms, 1, 1, 1, 1) for i in range(n)]


def test_profile_splits_runtime_and_trace_events(tmp_path):
    profile = load_profile(ROOT / "profiles" / "LoadedDiceFaceOff.toml")
    assert {e["name"] for e in profile.runtime_events()} == {"countdown_start", "match_end"}
    assert [e["scope"] for e in profile.scope_events()] == ["Client_OnGameStarted"]
    assert profile.phases["match_start"] == "match"
    assert profile.project_dir == Path("D:/UE5/LoadedDiceFaceOff")

    out = profile.write_events_json(tmp_path / "events.json")
    assert all("scope" not in e for e in json.loads(out.read_text()))


def test_load_markers_with_quoted_detail(tmp_path):
    csv = tmp_path / "markers.csv"
    csv.write_text('frame,t_ms,name,detail\n0,1.0,map_load_start,"/Game/Maps/A, B"\n12,120.0,run_end,""\n')
    markers = load_markers(csv)
    assert markers[0] == Marker(0, 1.0, "map_load_start", "/Game/Maps/A, B")
    assert markers[1].name == "run_end" and markers[1].detail == ""


def test_phases_follow_marker_order_on_the_same_frame():
    frames = frames_at(10)
    markers = [Marker(0, 0.1, "map_load_start"), Marker(0, 0.2, "map_load_end"), Marker(6, 60.0, "match_start")]
    phases = assign_phases(frames, markers, {"map_load_start": "loading", "map_load_end": "in_map", "match_start": "match"})
    assert phases[:6] == ["in_map"] * 6 and phases[6:] == ["match"] * 4


def test_trace_scope_becomes_marker_on_its_tick():
    frames = frames_at(5)
    windows = [FrameWindow(1.0 + i * 0.01, 1.01 + i * 0.01) for i in range(5)]
    markers = markers_from_trace_times({"Client_OnGameStarted": [1.025, 1.035]}, {"Client_OnGameStarted": "match_start"},
                                       frames, windows)
    assert markers == [Marker(2, 20.0, "match_start", "Client_OnGameStarted", source="trace")]


def test_hitch_gets_phase_and_time_since_last_marker():
    ms = [7.0] * 600
    ms[400] = 30.0
    frames = [Frame(i, i * 10.0, m, m - 1, 1, 1, 1) for i, m in enumerate(ms)]
    markers = [Marker(0, 0.0, "map_load_end"), Marker(380, 3800.0, "match_start")]
    result = analyze(frames, Thresholds(), markers, {"map_load_end": "in_map", "match_start": "match"})
    h = result.hitches[0]
    assert (h.phase, h.after_marker, h.after_marker_s) == ("match", "match_start", 0.2)
    assert [p.phase for p in result.phases] == ["in_map", "match"]
    assert result.phases[1].frames == 220


def test_playable_after_load_spikes_and_warmup_before_it():
    ms = [7.0] * 600
    ms[20] = 60.0   # level-load aftermath
    ms[60] = 25.0
    frames = [Frame(i, i * 10.0, m, m - 1, 1, 1, 1) for i, m in enumerate(ms)]
    markers = [Marker(0, 0.0, "map_load_start"), Marker(0, 300.0, "map_load_end")]
    result = analyze(frames, Thresholds(), markers, {"map_load_start": "loading", "map_load_end": "in_map"},
                     {"launch_to_recording_s": "3.0"})
    ld = result.loading
    assert ld.playable_frame == 61                  # first frame after the last spike
    assert ld.launch_to_playable_s == pytest.approx(3.0 + 0.61)
    assert ld.map_loaded_to_playable_s == pytest.approx(0.61 - 0.3)
    assert result.frame_phases[:61] == ["warmup"] * 61 and result.frame_phases[61] == "in_map"


def test_no_hitches_before_the_game_is_playable():
    ms = [7.0] * 600
    ms[30] = 40.0    # while the level is still settling: not a hitch
    ms[400] = 30.0   # once playable: a hitch
    frames = [Frame(i, i * 10.0, m, m - 1, 1, 1, 1) for i, m in enumerate(ms)]
    markers = [Marker(0, 0.0, "map_load_start"), Marker(0, 1.0, "map_load_end")]
    result = analyze(frames, Thresholds(), markers, {"map_load_start": "loading", "map_load_end": "in_map"})
    assert [h.start_frame for h in result.hitches] == [400]
