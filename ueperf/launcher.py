"""Starts a packaged build for one playtest run, watches it and gathers its outputs.

The plugin plays the game; this module launches it with the profile's settings,
takes a screenshot of the game window every `screenshot_every_s` seconds,
reads the plugin's status.json once a second and prints a line starting with
"ATTENTION" whenever the run looks stuck, so the supervising Claude agent knows
to look at the latest screenshot and step in with `ueperf send`.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ueperf import machine
from ueperf.profile import Profile
from ueperf.screenshot import ScreenshotError, screenshot_process

# Grace for engine start and map load on top of the play time before a run is killed.
STARTUP_TIMEOUT_S = 180.0
# The plugin's own time limit, a little past the launcher's, so the plugin normally ends the run cleanly.
PLUGIN_EXTRA_S = 150.0
# How long a state may last before the launcher asks the agent to look. Waiting for
# the start marker covers engine start and map load, so it gets longer.
STUCK_S = 10.0
WAITING_S = 60.0


@dataclass
class RunResult:
    run: Path
    ended: str = ""
    exit_code: int | None = None
    attention: list[str] = field(default_factory=list)


def default_build(profile: Profile) -> Path | None:
    """D:/UE5/PerfBuilds/<project>/Windows/<project>.exe, where the README's packaging command archives to."""
    if profile.build:
        return profile.build
    if not profile.project:
        return None
    name = profile.project.stem
    return profile.project.parent.parent / "PerfBuilds" / name / "Windows" / f"{name}.exe"


def game_binary(exe: Path) -> Path:
    """The real game executable behind a packaged build's launcher stub.

    A staged build's top-level <name>.exe only starts <name>/Binaries/Win64/<name>.exe
    as a child process, so the window and the exit code belong to that one.
    """
    inner = exe.parent / exe.stem / "Binaries" / "Win64" / exe.name
    return inner if inner.exists() else exe


def new_run_dir(root: Path, profile: Profile) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = root / f"{profile.path.stem}-{stamp}"
    run.mkdir(parents=True, exist_ok=False)
    return run


def game_command(profile: Profile, exe: Path, run: Path, game_map: str, play_s: float, trace: bool = True) -> list[str]:
    w, h = profile.resolution
    cmd = [str(exe), game_map, "-game", "-windowed", f"-ResX={w}", f"-ResY={h}", *profile.args]
    if trace:
        cmd += ["-trace=default,gpu", "-statnamedevents", f"-tracefile={run / 'capture.utrace'}"]
    cmd += [f"-PerfOut={run}", f"-PerfQuitAfter={play_s + PLUGIN_EXTRA_S:g}", f"-PerfEvents={run / 'events.json'}",
            f"-PerfPlay={run / 'play.json'}"]
    if profile.quit_on:
        cmd.append(f"-PerfQuitOn={profile.quit_on}")
    cmd += ["-log", f"-abslog={run / 'game.log'}"]
    return cmd


def read_status(run: Path) -> dict:
    try:
        return json.loads((run / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def send_command(run: Path, line: str) -> Path:
    """Append one agent command for the plugin's input driver (see FPerfInputDriver)."""
    path = run / "commands.txt"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.strip() + "\n")
    return path


def screenshot_times(run: Path, run_info: dict[str, str]) -> list[float]:
    """Screenshot times on the plugin clock (ms), from the launcher's wall clock and the plugin's start delay."""
    path = run / "screenshots.json"
    lead = run_info.get("launch_to_recording_s")
    if not path.exists() or not lead:
        return []
    shots = json.loads(path.read_text(encoding="utf-8"))
    return [(s["wall_s"] - float(lead)) * 1000.0 for s in shots]


def latest_run(root: Path) -> Path | None:
    marker = root / "latest.txt"
    if marker.exists():
        return Path(marker.read_text(encoding="utf-8").strip())
    runs = sorted((p for p in root.iterdir() if (p / "run.json").exists()), key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


class _Watch:
    """Turns status.json samples into ATTENTION lines for states that last STUCK_S or longer."""

    def __init__(self):
        self.since: dict[str, float] = {}
        self.raised: set[str] = set()
        self.last_frame, self.frame_since = -1, 0.0

    def check(self, status: dict, now: float) -> list[str]:
        if not status:
            return []
        frame = status.get("frame", -1)
        if frame != self.last_frame:
            self.last_frame, self.frame_since = frame, now
        states = {
            "no_pawn": status.get("driver") == "playing" and not status.get("pawn"),
            "cursor": status.get("driver") == "playing" and status.get("cursor"),
            "paused_game": bool(status.get("paused_game")),
            "waiting": status.get("driver") == "waiting",
            "frozen": now - self.frame_since >= STUCK_S,
            "no_actions": status.get("driver") != "waiting" and status.get("actions_allowed", 1) == 0,
        }
        text = {
            "no_pawn": "the player has no pawn (dead with no respawn, or spectating)",
            "cursor": "the mouse cursor is showing, so a menu or dialog may be open",
            "paused_game": "the game is paused",
            "waiting": "the input driver has not started: its start marker has not fired or no actions are bound yet",
            "frozen": "no new frames: the game may be hung or still loading",
            "no_actions": "no allowed input actions are bound right now",
        }
        out = []
        for key, on in states.items():
            if not on:
                self.since.pop(key, None)
                self.raised.discard(key)
                continue
            start = self.since.setdefault(key, now)
            if now - start >= (WAITING_S if key == "waiting" else STUCK_S) and key not in self.raised:
                self.raised.add(key)
                out.append(text[key])
        return out


def run_playtest(
    profile: Profile,
    runs_root: Path,
    *,
    game_map: str | None = None,
    play_s: float | None = None,
    exe: Path | None = None,
    trace: bool = True,
    screenshot_every_s: float | None = None,
    log=print,
) -> RunResult:
    exe = exe or default_build(profile)
    if exe is None or not exe.exists():
        raise FileNotFoundError(f"no packaged build at {exe}; package one (see README) or set build in the profile")
    exe = game_binary(exe.resolve())
    # The game runs from its own folder, so every path it is given must be absolute.
    runs_root = runs_root.resolve()
    game_map = game_map or (profile.maps[0] if profile.maps else "")
    play_s = play_s if play_s is not None else profile.play_s
    every = screenshot_every_s if screenshot_every_s is not None else profile.screenshot_every_s

    run = new_run_dir(runs_root, profile)
    (runs_root / "latest.txt").write_text(str(run), encoding="utf-8")
    profile.write_events_json(run / "events.json")
    commands = run / "commands.txt"
    commands.write_text("", encoding="utf-8")
    profile.write_play_json(run / "play.json", commands)
    # The driver ends the run after play_s seconds of play.
    play = json.loads((run / "play.json").read_text(encoding="utf-8"))
    play["play_s"] = play_s
    (run / "play.json").write_text(json.dumps(play, indent=2), encoding="utf-8")

    log("Collecting machine data ...")
    (run / "machine.json").write_text(json.dumps(machine.collect(), indent=2), encoding="utf-8")

    cmd = game_command(profile, exe, run, game_map, play_s, trace)
    meta = {
        "profile": str(profile.path), "profile_version": profile.version, "project": str(profile.project or ""),
        "build": str(exe), "build_time": datetime.fromtimestamp(exe.stat().st_mtime).isoformat(timespec="seconds"),
        "map": game_map, "play_s": play_s, "budget_ms": profile.budget_ms, "command": cmd,
        "started": datetime.now().isoformat(timespec="seconds"), "state": "running",
    }
    (run / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    log(f"Run folder {run}")
    log(f"Starting {exe.name} {game_map} for {play_s:g} s of play")
    launched = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=exe.parent)
    meta["pid"] = proc.pid
    (run / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    result = RunResult(run)
    watch = _Watch()
    shots = run / "screens"
    next_shot = launched + 5.0
    shot_log = []
    deadline = launched + play_s + STARTUP_TIMEOUT_S
    while proc.poll() is None:
        now = time.monotonic()
        status = read_status(run)
        for problem in watch.check(status, now):
            line = f"ATTENTION at {now - launched:.0f} s: {problem}. Look at the latest screenshot in {shots}."
            result.attention.append(line)
            log(line)
            next_shot = now  # a fresh screenshot to look at
        if every > 0 and now >= next_shot:
            name = f"{now - launched:06.1f}s.png"
            try:
                screenshot_process(proc.pid, shots / name)
                shot_log.append({"file": f"screens/{name}", "wall_s": round(now - launched, 2),
                                 "t_ms": status.get("t_ms"), "frame": status.get("frame")})
                (run / "screenshots.json").write_text(json.dumps(shot_log, indent=2), encoding="utf-8")
                log(f"screenshot {shots / name}  driver {status.get('driver', '?')}, "
                    f"{status.get('actions_fired', 0)}/{status.get('actions_allowed', 0)} actions fired")
            except ScreenshotError as e:
                log(f"screenshot skipped: {e}")
            next_shot = now + every
        if now > deadline:
            log(f"ATTENTION: the run passed {play_s + STARTUP_TIMEOUT_S:g} s; ending the game")
            result.attention.append("killed after timeout")
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            result.ended = "timeout"
            break
        time.sleep(1.0)

    result.exit_code = proc.wait()
    end_file = run / "run_end.txt"
    if not result.ended:
        result.ended = (end_file.read_text(encoding="utf-8").strip().partition("=")[2]
                        if end_file.exists() else "game exited")
    meta.update(state="done", ended=result.ended, exit_code=result.exit_code, attention=result.attention,
                finished=datetime.now().isoformat(timespec="seconds"), wall_s=round(time.monotonic() - launched, 1))
    (run / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log(f"Run ended: {result.ended} (exit code {result.exit_code})")
    return result
