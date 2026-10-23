"""Game profiles: how to run a project and which lifecycle events it has."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RUNTIME_KINDS = ("poll", "spawn", "log")

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


def load_profile(path: Path) -> Profile:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    phases = dict(DEFAULT_PHASES)
    phases.update(raw.get("phases", {}))
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
        lifecycle=[dict(e) for e in raw.get("lifecycle", [])],
        phases=phases,
    )
