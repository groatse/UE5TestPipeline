"""Game profiles: how to run a project and which lifecycle events it has."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RUNTIME_KINDS = ("poll", "spawn", "log")

# Actions that would leave gameplay (pause menus, quitting) are never fired unless
# the profile sets exclude_defaults = false.
DEFAULT_EXCLUDE = ["*Pause*", "*Menu*", "*Quit*", "*Exit*", "*Escape*"]
# Actions mapped to these keys are left out the same way: Escape opens menus in most games.
DEFAULT_EXCLUDE_KEYS = ["Escape"]


@dataclass
class InputRules:
    """How the plugin's input driver plays: the profile's [input] table."""
    start_on: str = "pawn_possessed"   # marker that starts play
    start_delay_s: float = 1.0
    exclude: list[str] = field(default_factory=list)
    exclude_keys: list[str] = field(default_factory=list)
    exclude_defaults: bool = True
    weights: dict[str, float] = field(default_factory=dict)
    voices: int = 2                    # actions played at the same time
    mouse_scale: float = 5.0           # per-frame delta for actions on a mouse axis
    seed: int = 0                      # 0: a new seed each run

    def all_excluded(self) -> list[str]:
        return (DEFAULT_EXCLUDE if self.exclude_defaults else []) + self.exclude

    def all_excluded_keys(self) -> list[str]:
        return (DEFAULT_EXCLUDE_KEYS if self.exclude_defaults else []) + self.exclude_keys

DEFAULT_PHASES = {
    "map_load_start": "loading",
    "map_load_end": "in_map",
    "match_start": "match",
    "match_end": "post_match",
}


@dataclass
class Profile:
    path: Path
    project: Path | None = None
    version: int = 1
    maps: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    budget_ms: float | None = None
    runs: int = 3
    play_s: float = 30.0
    quit_on: str = ""
    build: Path | None = None            # packaged game executable
    resolution: tuple[int, int] = (1920, 1080)
    screenshot_every_s: float = 30.0
    input: InputRules = field(default_factory=InputRules)
    lifecycle: list[dict] = field(default_factory=list)
    phases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PHASES))

    @property
    def project_dir(self) -> Path | None:
        return self.project.parent if self.project else None

    def runtime_events(self) -> list[dict]:
        """Lifecycle entries the plugin detects while the game runs."""
        return [e for e in self.lifecycle if any(k in e for k in RUNTIME_KINDS)]

    def scope_events(self) -> list[dict]:
        """Lifecycle entries the analyzer finds in the trace afterwards."""
        return [e for e in self.lifecycle if "scope" in e]

    def write_events_json(self, out: Path) -> Path:
        """The -PerfEvents file the plugin reads."""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.runtime_events(), indent=2), encoding="utf-8")
        return out

    def write_play_json(self, out: Path, commands: Path | None = None, seed: int | None = None) -> Path:
        """The -PerfPlay file the plugin's input driver reads."""
        rules = self.input
        play = {
            "start_on": rules.start_on,
            "start_delay_s": rules.start_delay_s,
            "exclude": rules.all_excluded(),
            "exclude_keys": rules.all_excluded_keys(),
            "weights": rules.weights,
            "voices": rules.voices,
            "mouse_scale": rules.mouse_scale,
            "seed": rules.seed if seed is None else seed,
        }
        if commands is not None:
            play["commands"] = str(commands)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(play, indent=2), encoding="utf-8")
        return out


def load_profile(path: Path) -> Profile:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    phases = dict(DEFAULT_PHASES)
    phases.update(raw.get("phases", {}))
    rules = raw.get("input", {})
    defaults = InputRules()
    return Profile(
        path=Path(path),
        project=Path(raw["project"]) if raw.get("project") else None,
        version=int(raw.get("version", 1)),
        maps=list(raw.get("maps", [])),
        args=list(raw.get("args", [])),
        budget_ms=float(raw["budget_ms"]) if "budget_ms" in raw else None,
        runs=int(raw.get("runs", 3)),
        play_s=float(raw.get("play_s", 30.0)),
        quit_on=str(raw.get("quit_on", "")),
        build=Path(raw["build"]) if raw.get("build") else None,
        resolution=tuple(raw.get("resolution", (1920, 1080))),
        screenshot_every_s=float(raw.get("screenshot_every_s", 30.0)),
        input=InputRules(
            start_on=str(rules.get("start_on", defaults.start_on)),
            start_delay_s=float(rules.get("start_delay_s", defaults.start_delay_s)),
            exclude=list(rules.get("exclude", [])),
            exclude_keys=list(rules.get("exclude_keys", [])),
            exclude_defaults=bool(rules.get("exclude_defaults", True)),
            weights={k: float(v) for k, v in rules.get("weights", {}).items()},
            voices=int(rules.get("voices", defaults.voices)),
            mouse_scale=float(rules.get("mouse_scale", defaults.mouse_scale)),
            seed=int(rules.get("seed", 0)),
        ),
        lifecycle=[dict(e) for e in raw.get("lifecycle", [])],
        phases=phases,
    )
