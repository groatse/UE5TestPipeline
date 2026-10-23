import json
import struct
import zlib
from pathlib import Path

from ueperf.analyze import analyze
from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.inputs import InputEvent, active_between, describe, load_coverage, load_inputs
from ueperf.launcher import _Watch, game_binary, game_command, screenshot_times
from ueperf.onboard import draft_profile
from ueperf.profile import load_profile
from ueperf.screenshot import downscale, write_png

ROOT = Path(__file__).parent.parent

INPUTS_CSV = """frame,t_ms,action,gesture,value,duration_ms
95,1817.3,"<driver>",start,"9 actions",0.0
95,1817.4,"Play_Look",sweep,"-4.61;-1.93",817.7
95,1817.5,"Play_Slap",tap,"1.00",144.6
400,4000.0,"<agent>",key,"Enter",0.0
410,4100.0,"Play_Throw",hold,"1.00",900.0
"""

ACTIONS_CSV = """# seed=42 input=enhanced
name,value_type,keys,source,excluded,weight,fired,active_s
"Play_Look",axis2d,"MouseX MouseY","mapping context",0,3.00,13,20.5
"Play_Slap",bool,"A","mapping context",0,1.00,6,1.2
"Play_Throw",bool,"SpaceBar","mapping context",0,1.00,0,0.0
"Play_AccessMenu",bool,"M Escape","mapping context",1,1.00,0,0.0
"""


def test_inputs_and_coverage_load(tmp_path):
    (tmp_path / "inputs.csv").write_text(INPUTS_CSV)
    (tmp_path / "actions.csv").write_text(ACTIONS_CSV)
    events = load_inputs(tmp_path / "inputs.csv")
    assert len(events) == 5 and events[1].action == "Play_Look" and events[1].end_ms == 1817.4 + 817.7

    cov = load_coverage(tmp_path / "actions.csv")
    assert cov.seed == "42" and cov.input_system == "enhanced"
    assert [a.name for a in cov.allowed] == ["Play_Look", "Play_Slap", "Play_Throw"]
    assert [a.name for a in cov.fired] == ["Play_Look", "Play_Slap"]
    assert cov.share == 2 / 3


def test_active_between_keeps_gestures_down_in_the_window(tmp_path):
    (tmp_path / "inputs.csv").write_text(INPUTS_CSV)
    events = load_inputs(tmp_path / "inputs.csv")
    # The slap ended at 1962 ms, the look sweep at 2635 ms; driver and agent rows never count.
    assert [e.action for e in active_between(events, 2000.0, 2100.0)] == ["Play_Look"]
    assert [e.action for e in active_between(events, 1900.0, 4200.0)] == ["Play_Look", "Play_Slap", "Play_Throw"]
    assert describe(events[1]) == "Play_Look sweep (-4.61;-1.93) 0.82 s"
    assert describe(events[2]) == "Play_Slap tap"


def test_hitch_carries_input_coverage_and_screenshot():
    ms = [7.0] * 600
    ms[400] = 30.0
    frames = [Frame(i, i * 10.0, m, m - 1, 1, 1, 1) for i, m in enumerate(ms)]
    inputs = [InputEvent(380, 3500.0, "Play_Throw", "hold", "1.00", 600.0),
              InputEvent(100, 1000.0, "Play_Slap", "tap", "1.00", 100.0),
              InputEvent(390, 3900.0, "<agent>", "key", "Enter", 0.0)]
    result = analyze(frames, Thresholds(), inputs=inputs, screenshot_ms=[4050.0])
    (h,) = result.hitches
    assert h.inputs == ("Play_Throw hold 0.60 s",)
    assert h.screenshot_s == 0.05
    assert result.agent_commands == [{"t_ms": 3900.0, "frame": 390, "command": "key Enter"}]


def test_profile_writes_play_config(tmp_path):
    profile = load_profile(ROOT / "profiles" / "LoadedDiceFaceOff.toml")
    play = json.loads(profile.write_play_json(tmp_path / "play.json", tmp_path / "commands.txt").read_text())
    assert play["start_on"] == "pawn_possessed"
    assert "*Voice*" in play["exclude"] and "*Menu*" in play["exclude"]   # profile and defaults
    assert play["exclude_keys"] == ["Escape"]
    assert play["weights"] == {"Play_Look": 3.0}
    assert play["commands"] == str(tmp_path / "commands.txt")


def test_game_command_uses_absolute_run_paths(tmp_path):
    profile = load_profile(ROOT / "profiles" / "LoadedDiceFaceOff.toml")
    cmd = game_command(profile, Path("D:/Builds/Game.exe"), tmp_path, "/Game/EvilLair", 30)
    assert cmd[:3] == [str(Path("D:/Builds/Game.exe")), "/Game/EvilLair", "-game"]
    assert f"-PerfPlay={tmp_path / 'play.json'}" in cmd and "-PerfQuitOn=match_end" in cmd
    assert f"-tracefile={tmp_path / 'capture.utrace'}" in cmd


def test_game_binary_skips_the_launcher_stub(tmp_path):
    stub = tmp_path / "Game.exe"
    stub.write_bytes(b"")
    assert game_binary(stub) == stub
    inner = tmp_path / "Game" / "Binaries" / "Win64" / "Game.exe"
    inner.parent.mkdir(parents=True)
    inner.write_bytes(b"")
    assert game_binary(stub) == inner


def test_screenshot_times_move_to_the_plugin_clock(tmp_path):
    (tmp_path / "screenshots.json").write_text(json.dumps([{"file": "a.png", "wall_s": 12.5}]))
    assert screenshot_times(tmp_path, {"launch_to_recording_s": "2.5"}) == [10000.0]
    assert screenshot_times(tmp_path, {}) == []


def test_watch_raises_each_stuck_state_once():
    watch = _Watch()
    playing = {"frame": 1, "driver": "playing", "pawn": "", "cursor": False, "actions_allowed": 9}
    assert watch.check(playing, 0.0) == []
    assert watch.check({**playing, "frame": 2}, 5.0) == []
    raised = watch.check({**playing, "frame": 3}, 11.0)
    assert raised == ["the player has no pawn (dead with no respawn, or spectating)"]
    assert watch.check({**playing, "frame": 4}, 12.0) == []
    # Back to normal, then stuck again: raised again.
    watch.check({**playing, "frame": 5, "pawn": "BP_Pawn_C"}, 13.0)
    watch.check({**playing, "frame": 6}, 14.0)
    assert watch.check({**playing, "frame": 7}, 25.0) != []


def test_watch_waits_longer_for_the_start_marker():
    watch = _Watch()
    waiting = {"driver": "waiting", "actions_allowed": 0}
    for t in range(0, 40, 2):
        assert watch.check({**waiting, "frame": t}, float(t)) == []
    assert watch.check({**waiting, "frame": 99}, 61.0) != []


def test_png_round_trip_header_and_downscale(tmp_path):
    w, h = 4, 2
    rgb = bytes(range(w * h * 3))
    nw, nh, small = downscale(w, h, rgb, 2)
    assert (nw, nh) == (2, 1) and small == rgb[0:3] + rgb[6:9]
    png = write_png(tmp_path / "s.png", w, h, rgb).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (w, h)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\0" + rgb[:12] + b"\0" + rgb[12:]


def test_onboard_drafts_a_profile(tmp_path):
    project = tmp_path / "Game" / "Game.uproject"
    (project.parent / "Content" / "Maps").mkdir(parents=True)
    (project.parent / "Config").mkdir()
    (project.parent / "Source" / "Game").mkdir(parents=True)
    project.write_text(json.dumps({"Plugins": [{"Name": "OnlineSubsystemSteam", "Enabled": True}]}))
    for name in ("MainMenu", "Arena"):
        (project.parent / "Content" / "Maps" / f"{name}.umap").write_bytes(b"")
    (project.parent / "Content" / "IA_Jump.uasset").write_bytes(b"/Script/EnhancedInput\0InputAction\0")
    (project.parent / "Content" / "IA_PauseMenu.uasset").write_bytes(b"/Script/EnhancedInput\0InputAction\0")
    (project.parent / "Config" / "DefaultEngine.ini").write_text("GameDefaultMap=/Game/Maps/MainMenu.MainMenu\n")
    (project.parent / "Source" / "Game" / "MyGameMode.h").write_text(
        "class GAME_API AMyGameMode : public AGameModeBase {\n UFUNCTION(BlueprintPure)\n bool HasMatchEnded() const;\n};")

    text = draft_profile(project)
    profile_path = tmp_path / "Game.toml"
    profile_path.write_text(text)
    profile = load_profile(profile_path)
    assert profile.maps == ["/Game/Maps/Arena"]
    assert "-nosteam" in profile.args
    assert profile.input.exclude == ["IA_PauseMenu"]
    assert "GameMode.HasMatchEnded" in text and "IA_Jump" in text
