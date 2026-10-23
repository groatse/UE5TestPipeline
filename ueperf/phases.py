"""Lifecycle phases: which part of the run each frame belongs to."""

from __future__ import annotations

from ueperf.ingest.frames import Frame
from ueperf.ingest.markers import Marker

STARTUP = "startup"
# Phases whose frames are not gameplay: kept out of baselines and the gains ranking.
NON_GAMEPLAY = {STARTUP, "loading"}


def assign_phases(frames: list[Frame], markers: list[Marker], phase_map: dict[str, str]) -> list[str]:
    """The phase of each frame: the phase started by the latest phase marker at or before it."""
    # Markers on the same frame keep the order they fired in.
    ordered = sorted((m for m in markers if m.name in phase_map), key=lambda m: (m.frame, m.t_ms))
    changes = [(m.frame, phase_map[m.name]) for m in ordered]
    phases, current, k = [], STARTUP, 0
    for f in frames:
        while k < len(changes) and changes[k][0] <= f.frame:
            current = changes[k][1]
            k += 1
        phases.append(current)
    return phases


def last_marker_before(markers: list[Marker], frame: int) -> Marker | None:
    before = [m for m in markers if m.frame <= frame]
    return max(before, key=lambda m: (m.frame, m.t_ms)) if before else None
