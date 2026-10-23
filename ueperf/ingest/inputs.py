"""What the input driver did: inputs.csv (one row per gesture or agent command) and actions.csv."""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from pathlib import Path

DRIVER = "<driver>"
AGENT = "<agent>"


@dataclass(frozen=True)
class InputEvent:
    frame: int
    t_ms: float          # plugin clock, as in frames.csv
    action: str          # action name, or <driver> / <agent>
    gesture: str         # tap, double_tap, hold, direction, sweep; or the driver or agent verb
    value: str
    duration_ms: float

    @property
    def end_ms(self) -> float:
        return self.t_ms + self.duration_ms


@dataclass(frozen=True)
class ActionInfo:
    name: str
    value_type: str      # bool, axis1d, axis2d, axis3d
    keys: str
    source: str
    excluded: bool
    weight: float
    fired: int
    active_s: float


@dataclass(frozen=True)
class Coverage:
    seed: str
    input_system: str
    actions: list[ActionInfo]

    @property
    def allowed(self) -> list[ActionInfo]:
        return [a for a in self.actions if not a.excluded]

    @property
    def fired(self) -> list[ActionInfo]:
        return [a for a in self.allowed if a.fired > 0]

    @property
    def share(self) -> float:
        return len(self.fired) / len(self.allowed) if self.allowed else 0.0


def load_inputs(path: Path) -> list[InputEvent]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            InputEvent(int(r["frame"]), float(r["t_ms"]), r["action"], r["gesture"], r.get("value", ""),
                       float(r.get("duration_ms") or 0.0))
            for r in csv.DictReader(f)
        ]


def load_coverage(path: Path) -> Coverage | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = {}
    if lines and lines[0].startswith("#"):
        for part in lines.pop(0)[1:].split():
            k, _, v = part.partition("=")
            header[k] = v
    actions = [
        ActionInfo(r["name"], r["value_type"], r["keys"], r["source"], r["excluded"] == "1", float(r["weight"]),
                   int(r["fired"]), float(r["active_s"]))
        for r in csv.DictReader(lines)
    ]
    return Coverage(header.get("seed", ""), header.get("input", ""), actions)


def gestures(events: list[InputEvent]) -> list[InputEvent]:
    """Only the driver's gestures, without its start row and agent commands."""
    return [e for e in events if e.action not in (DRIVER, AGENT)]


def active_between(events: list[InputEvent], start_ms: float, end_ms: float) -> list[InputEvent]:
    """Gestures that were down at any point from `start_ms` to `end_ms`, in start order.

    `events` must be sorted by start time, as the plugin writes them.
    """
    starts = [e.t_ms for e in events]
    hi = bisect.bisect_right(starts, end_ms)
    return [e for e in events[:hi] if e.end_ms >= start_ms and e.action not in (DRIVER, AGENT)]


def describe(event: InputEvent) -> str:
    """Short text for one gesture, such as 'ThrowAction tap' or 'LookAction sweep (5.00;0.00) 1.20 s'."""
    value = f" ({event.value})" if event.value and event.value not in ("1.00", "") else ""
    length = f" {event.duration_ms / 1000:.2f} s" if event.duration_ms >= 250 else ""
    return f"{event.action} {event.gesture}{value}{length}"
