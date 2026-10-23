"""Analyzer thresholds, loaded from a TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class Thresholds:
    budget_ms: float = 11.1
    playable_window_s: float = 2.0
    playable_max_share_over: float = 0.01
    playable_max_frame_ratio: float = 1.5
    mixed_within: float = 0.10
    overrun_min_frames: int = 45
    overrun_max_gap_frames: int = 5
    hitch_factor: float = 2.0
    hitch_window_frames: int = 90
    hitch_min_window_frames: int = 10
    hitch_max_frames: int = 3
    hitch_require_over_budget: bool = True
    causes_top: int = 5
    causes_baseline_frames: int = 30
    causes_max_frames: int = 150
    causes_skip_start_s: float = 2.0
    causes_min_rise_ms: float = 0.05
    causes_ignore: tuple[str, ...] = field(default_factory=tuple)
    causes_trim: tuple[str, ...] = field(default_factory=tuple)
    causes_tree_below_share: float = 0.5
    causes_requesters: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
    causes = raw.get("causes", {})
    playable = raw.get("playable", {})
    defaults = Thresholds()
    return Thresholds(
        budget_ms=float(budget_ms if budget_ms is not None else raw.get("budget_ms", defaults.budget_ms)),
        playable_window_s=float(playable.get("window_s", defaults.playable_window_s)),
        playable_max_share_over=float(playable.get("max_share_over", defaults.playable_max_share_over)),
        playable_max_frame_ratio=float(playable.get("max_frame_ratio", defaults.playable_max_frame_ratio)),
        mixed_within=float(bound.get("mixed_within", defaults.mixed_within)),
        overrun_min_frames=int(overrun.get("min_frames", defaults.overrun_min_frames)),
        overrun_max_gap_frames=int(overrun.get("max_gap_frames", defaults.overrun_max_gap_frames)),
        hitch_factor=float(hitch.get("factor", defaults.hitch_factor)),
        hitch_window_frames=int(hitch.get("window_frames", defaults.hitch_window_frames)),
        hitch_min_window_frames=int(hitch.get("min_window_frames", defaults.hitch_min_window_frames)),
        hitch_max_frames=int(hitch.get("max_frames", defaults.hitch_max_frames)),
        hitch_require_over_budget=bool(hitch.get("require_over_budget", defaults.hitch_require_over_budget)),
        causes_top=int(causes.get("top", defaults.causes_top)),
        causes_baseline_frames=int(causes.get("baseline_frames", defaults.causes_baseline_frames)),
        causes_max_frames=int(causes.get("max_frames", defaults.causes_max_frames)),
        causes_skip_start_s=float(causes.get("skip_start_s", defaults.causes_skip_start_s)),
        causes_min_rise_ms=float(causes.get("min_rise_ms", defaults.causes_min_rise_ms)),
        causes_ignore=tuple(causes.get("ignore", defaults.causes_ignore)),
        causes_trim=tuple(causes.get("trim", defaults.causes_trim)),
        causes_tree_below_share=float(causes.get("tree_below_share", defaults.causes_tree_below_share)),
        causes_requesters={k: tuple(v) for k, v in causes.get("requesters", {}).items()},
    )
