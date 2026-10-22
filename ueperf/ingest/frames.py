"""Reads the PerfPipeline plugin's frames.csv into Frame records."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = ("frame", "t_ms", "frame_ms", "game_ms", "render_ms", "rhi_ms", "gpu_ms")


@dataclass(frozen=True)
class Frame:
    frame: int
    t_ms: float
    frame_ms: float
    game_ms: float
    render_ms: float
    rhi_ms: float
    gpu_ms: float
    compositor_gpu_ms: float = 0.0

    def threads(self) -> dict[str, float]:
        """Thread and GPU times by name, in the order used for bound type."""
        return {"game": self.game_ms, "render": self.render_ms, "rhi": self.rhi_ms, "gpu": self.gpu_ms}


def load_frames(path: Path) -> list[Frame]:
    """Load frames.csv. Raises ValueError if a required column is missing.

    The plugin writes the engine's delta time as frame_ms, which at the end of
    frame N is the duration of frame N-1. The thread times in the same row
    belong to frame N. Each Frame here pairs a frame's thread times with its
    own duration, taken from the next row, so the last row is dropped.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {', '.join(missing)}")
        has_compositor = "compositor_gpu_ms" in (reader.fieldnames or [])
        rows = list(reader)

    return [
        Frame(
            frame=int(row["frame"]),
            t_ms=float(row["t_ms"]),
            frame_ms=float(following["frame_ms"]),
            game_ms=float(row["game_ms"]),
            render_ms=float(row["render_ms"]),
            rhi_ms=float(row["rhi_ms"]),
            gpu_ms=float(row["gpu_ms"]),
            compositor_gpu_ms=float(row["compositor_gpu_ms"] or 0.0) if has_compositor else 0.0,
        )
        for row, following in zip(rows, rows[1:])
    ]
