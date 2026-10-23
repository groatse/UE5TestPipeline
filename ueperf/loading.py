"""Loading time: how long until the game reaches a steady in-budget frame rate."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.markers import Marker


@dataclass(frozen=True)
class Loading:
    """Times in seconds. None when the run doesn't show it (no marker, or never steady)."""
    launch_to_recording_s: float | None    # process launch to plugin start, from run_info.txt
    map_load_s: float | None               # map_load_start to map_load_end
    playable_frame: int | None             # first frame of the first steady window
    playable_run_s: float | None           # that frame on the run clock
    map_loaded_to_playable_s: float | None
    launch_to_playable_s: float | None
    window_s: float
    max_share_over: float
    max_frame_ratio: float = 1.5


def read_run_info(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    info = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key.strip()] = value.strip()
    return info


def find_playable(
    frames: list[Frame], start: int, budget_ms: float, window_s: float, max_share_over: float, max_frame_ratio: float,
) -> int | None:
    """First frame at or after `start` that begins a `window_s` stretch with at most
    `max_share_over` of its frames over budget and none over `max_frame_ratio` times it."""
    cost = [f.frame_ms + f.compositor_gpu_ms for f in frames]
    window_ms = window_s * 1000.0
    big = budget_ms * max_frame_ratio
    end, over, spikes = start, 0, deque()  # spikes: indices of frames over `big` inside the window
    for i in range(start, len(frames)):
        while end < len(frames) and frames[end].t_ms - frames[i].t_ms < window_ms:
            over += cost[end] > budget_ms
            if cost[end] > big:
                spikes.append(end)
            end += 1
        if end == len(frames) and frames[-1].t_ms - frames[i].t_ms < window_ms:
            return None  # not enough run left to judge
        while spikes and spikes[0] < i:
            spikes.popleft()
        if not spikes and over <= max_share_over * (end - i):
            return i
        over -= cost[i] > budget_ms
    return None


def measure_loading(frames: list[Frame], markers: list[Marker], t: Thresholds, run_info: dict[str, str]) -> Loading:
    load_start = next((m for m in markers if m.name == "map_load_start"), None)
    load_end = next((m for m in markers if m.name == "map_load_end"), None)
    start = 0
    if load_end:
        start = next((i for i, f in enumerate(frames) if f.frame >= load_end.frame), 0)

    playable = find_playable(frames, start, t.budget_ms, t.playable_window_s, t.playable_max_share_over,
                             t.playable_max_frame_ratio)
    launch = run_info.get("launch_to_recording_s")
    launch_s = float(launch) if launch else None
    playable_run_s = frames[playable].t_ms / 1000.0 if playable is not None else None

    return Loading(
        launch_to_recording_s=launch_s,
        map_load_s=round((load_end.t_ms - load_start.t_ms) / 1000.0, 3) if load_start and load_end else None,
        playable_frame=frames[playable].frame if playable is not None else None,
        playable_run_s=round(playable_run_s, 3) if playable_run_s is not None else None,
        map_loaded_to_playable_s=(round(playable_run_s - load_end.t_ms / 1000.0, 3)
                                  if playable_run_s is not None and load_end else None),
        launch_to_playable_s=(round(launch_s + playable_run_s, 3)
                              if playable_run_s is not None and launch_s is not None else None),
        window_s=t.playable_window_s,
        max_share_over=t.playable_max_share_over,
        max_frame_ratio=t.playable_max_frame_ratio,
    )
