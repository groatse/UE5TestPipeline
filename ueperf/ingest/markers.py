"""Lifecycle markers: from the plugin's markers.csv, and from trace scopes."""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from pathlib import Path

from ueperf.ingest.frames import Frame


@dataclass(frozen=True)
class Marker:
    frame: int        # recorded frame number, as in frames.csv
    t_ms: float       # plugin clock
    name: str
    detail: str = ""
    source: str = "runtime"  # "runtime" (plugin) or "trace" (found in the trace afterwards)


def load_markers(path: Path) -> list[Marker]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            Marker(int(row["frame"]), float(row["t_ms"]), row["name"], row.get("detail", ""))
            for row in csv.DictReader(f)
        ]


def markers_from_trace_times(
    times: dict[str, list[float]],
    names: dict[str, str],
    frames: list[Frame],
    windows: list,
    repeat: dict[str, bool] | None = None,
) -> list[Marker]:
    """Turn trace times of scopes into markers on the frames whose tick contains them.

    `times` maps scope name to its start times on the trace clock, `names` maps
    scope name to marker name. Only the first occurrence is kept unless
    `repeat` says otherwise.
    """
    aligned = [(w.start_s, i) for i, w in enumerate(windows) if w is not None]
    starts = [s for s, _ in aligned]
    out = []
    for scope, marker_name in names.items():
        found = sorted(times.get(scope, []))
        if not (repeat or {}).get(scope, False):
            found = found[:1]
        for t in found:
            pos = bisect.bisect_right(starts, t) - 1
            if pos < 0:
                continue
            i = aligned[pos][1]
            out.append(Marker(frames[i].frame, frames[i].t_ms, marker_name, scope, source="trace"))
    return out
