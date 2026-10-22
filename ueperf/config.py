"""Analyzer thresholds, loaded from a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Thresholds:
    budget_ms: float = 11.1
    mixed_within: float = 0.10
    overrun_min_frames: int = 45
    overrun_max_gap_frames: int = 5
    hitch_factor: float = 2.0
    hitch_window_frames: int = 90
    hitch_min_window_frames: int = 10
    hitch_max_frames: int = 3
    hitch_require_over_budget: bool = True


def load_thresholds(path: Path | None = None, budget_ms: float | None = None) -> Thresholds:
    """Load thresholds from `path`, or the packaged defaults when it is None.

    `budget_ms`, when given, overrides the file's budget.
    """
    if path is None:
        text = resources.files("ueperf").joinpath("thresholds.toml").read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    raw = tomllib.loads(text)

    bound = raw.get("bound", {})
    overrun = raw.get("overrun", {})
    hitch = raw.get("hitch", {})
    defaults = Thresholds()
    return Thresholds(
        budget_ms=float(budget_ms if budget_ms is not None else raw.get("budget_ms", defaults.budget_ms)),
        mixed_within=float(bound.get("mixed_within", defaults.mixed_within)),
        overrun_min_frames=int(overrun.get("min_frames", defaults.overrun_min_frames)),
        overrun_max_gap_frames=int(overrun.get("max_gap_frames", defaults.overrun_max_gap_frames)),
        hitch_factor=float(hitch.get("factor", defaults.hitch_factor)),
        hitch_window_frames=int(hitch.get("window_frames", defaults.hitch_window_frames)),
        hitch_min_window_frames=int(hitch.get("min_window_frames", defaults.hitch_min_window_frames)),
        hitch_max_frames=int(hitch.get("max_frames", defaults.hitch_max_frames)),
        hitch_require_over_budget=bool(hitch.get("require_over_budget", defaults.hitch_require_over_budget)),
    )
