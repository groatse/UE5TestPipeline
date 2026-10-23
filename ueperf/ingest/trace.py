"""Pulls timing events out of a capture's .utrace through headless Unreal Insights.

Trace times are seconds since process launch; the plugin's frames.csv times are
milliseconds since plugin startup. `align_frames` matches the two by lining up
each recorded frame with a game thread FEngineLoop::Tick of the same length.
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ueperf.ingest.frames import Frame
from ueperf.insights import InsightsError, run_commands

TICK_TIMER = "FEngineLoop::Tick"
EXPORT_THREADS = "GameThread,RenderThread*,RHIThread,GPU*,*Worker*"
EVENT_COLUMNS = "ThreadName,TimerName,StartTime,EndTime,Depth"

# Largest index offset tried when lining frames up with ticks.
MAX_ALIGN_OFFSET = 200
# Median difference between a frame's duration and its tick's, beyond which
# alignment is treated as failed.
MAX_ALIGN_ERROR_MS = 1.0


@dataclass(frozen=True)
class FrameWindow:
    """A recorded frame's game thread tick on the trace clock, in seconds."""
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Event:
    thread: str
    timer: str
    start_s: float
    end_s: float
    depth: int


def thread_group(thread_name: str) -> str | None:
    """Map a trace thread name to the bound-type name it belongs to.

    Task graph worker threads are grouped as "workers"; their time is summed
    across all workers, so it can exceed the frame's wall time.
    """
    if thread_name == "GameThread":
        return "game"
    if thread_name.startswith("RenderThread"):
        return "render"
    if thread_name == "RHIThread":
        return "rhi"
    if thread_name.startswith("GPU"):
        return "gpu"
    if "Worker" in thread_name:
        return "workers"
    return None


def _read_events(path: Path) -> list[Event]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [
            Event(
                thread=row["ThreadName"],
                timer=row["TimerName"],
                start_s=float(row["StartTime"]),
                end_s=float(row["EndTime"]),
                depth=int(row["Depth"]),
            )
            for row in csv.DictReader(f)
        ]


def export_ticks(insights: Path, trace: Path, workdir: Path) -> list[FrameWindow]:
    """Every top-level game thread tick in the trace, in order."""
    return export_ticks_and_scopes(insights, trace, workdir, [])[0]


def export_ticks_and_scopes(
    insights: Path, trace: Path, workdir: Path, scopes: list[str],
) -> tuple[list[FrameWindow], dict[str, list[float]]]:
    """Game thread ticks, and the start times of the named scopes on any thread, in one Insights run."""
    ticks_out = workdir / "ticks.csv"
    scopes_out = workdir / "scopes.csv"
    commands = [f"TimingInsights.ExportTimingEvents {ticks_out} -columns={EVENT_COLUMNS} -threads=GameThread -timers={TICK_TIMER}"]
    if scopes:
        commands.append(f"TimingInsights.ExportTimingEvents {scopes_out} -columns={EVENT_COLUMNS} -timers={','.join(scopes)}")
    run_commands(insights, trace, commands, workdir)
    if not ticks_out.exists():
        raise InsightsError(f"Insights wrote no tick export; see {workdir / 'insights.log'}")

    events = [e for e in _read_events(ticks_out) if e.depth == 0 and e.timer == TICK_TIMER]
    events.sort(key=lambda e: e.start_s)
    times: dict[str, list[float]] = defaultdict(list)
    if scopes and scopes_out.exists():
        for e in _read_events(scopes_out):
            if e.timer in scopes:
                times[e.timer].append(e.start_s)
    return [FrameWindow(e.start_s, e.end_s) for e in events], dict(times)


def align_frames(frames: list[Frame], ticks: list[FrameWindow]) -> list[FrameWindow | None]:
    """The tick window of each frame, found by matching frame durations to tick durations.

    Raises InsightsError when no offset lines the two sequences up closely.
    """
    tick_ms = [(t.end_s - t.start_s) * 1000.0 for t in ticks]
    frame_ms = [f.frame_ms for f in frames]

    best_offset, best_error = None, float("inf")
    for offset in range(-MAX_ALIGN_OFFSET, MAX_ALIGN_OFFSET + 1):
        diffs = [
            abs(ms - tick_ms[i + offset])
            for i, ms in enumerate(frame_ms)
            if 0 <= i + offset < len(tick_ms)
        ]
        if len(diffs) < len(frame_ms) // 2:
            continue
        error = statistics.median(diffs)
        if error < best_error:
            best_offset, best_error = offset, error

    if best_offset is None or best_error > MAX_ALIGN_ERROR_MS:
        raise InsightsError(f"could not line frames up with trace ticks (median error {best_error:.2f} ms)")

    return [
        ticks[i + best_offset] if 0 <= i + best_offset < len(ticks) else None
        for i in range(len(frames))
    ]


def window_for(windows: list[FrameWindow | None], index: int) -> tuple[float, float] | None:
    """Export interval for frame `index`: its tick plus the next one.

    The render thread, RHI thread and GPU work on a frame during the following
    tick, so the interval covers both.
    """
    current = windows[index]
    if current is None:
        return None
    following = windows[index + 1] if index + 1 < len(windows) else None
    return current.start_s, (following.end_s if following else current.end_s)


def export_frame_events(
    insights: Path,
    trace: Path,
    windows: list[FrameWindow | None],
    indices: list[int],
    workdir: Path,
) -> dict[int, list[Event]]:
    """Timing events on the bound-type threads for each frame in `indices`, from one Insights run."""
    commands, paths = [], {}
    for index in indices:
        interval = window_for(windows, index)
        if interval is None:
            continue
        out = workdir / f"frame_{index}.csv"
        paths[index] = out
        commands.append(
            f"TimingInsights.ExportTimingEvents {out} -columns={EVENT_COLUMNS} -threads={EXPORT_THREADS} "
            f"-timers=* -startTime={interval[0]:.6f} -endTime={interval[1]:.6f}"
        )
    if commands:
        run_commands(insights, trace, commands, workdir)

    events = {}
    for index, path in paths.items():
        if path.exists():
            events[index] = _read_events(path)
            path.unlink()
    return events


Path = tuple[str, ...]


def path_times(events: list[Event], windows: list[FrameWindow | None], index: int) -> dict[tuple[str, Path], list[float]]:
    """Inclusive and exclusive ms per (thread group, call path) for one frame.

    A call path is the chain of scope names from the thread's top-level scope
    down to the scope itself. Calls on the same path are summed, and paths on
    different threads of a group (render threads, workers) are merged. Times
    are clipped to the frame's interval as in `exclusive_ms`.
    """
    interval = window_for(windows, index)
    own = windows[index]
    if interval is None or own is None:
        return {}

    by_thread: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if thread_group(e.thread):
            by_thread[e.thread].append(e)

    totals: dict[tuple[str, Path], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for thread, thread_events in by_thread.items():
        group = thread_group(thread)
        lo, hi = (own.start_s, own.end_s) if group == "game" else interval
        thread_events.sort(key=lambda e: (e.start_s, e.depth))

        stack: list[list] = []  # [event, path, child time]

        def close(item: list) -> None:
            event, path, child = item
            clipped = max(0.0, min(event.end_s, hi) - max(event.start_s, lo))
            t = totals[(group, path)]
            t[0] += clipped * 1000.0
            t[1] += max(0.0, clipped - child) * 1000.0
            if stack:
                stack[-1][2] += clipped

        for e in thread_events:
            while stack and stack[-1][0].depth >= e.depth:
                close(stack.pop())
            parent_path = stack[-1][1] if stack else ()
            stack.append([e, parent_path + (e.timer,), 0.0])
        while stack:
            close(stack.pop())

    return dict(totals)


def export_timers(insights: Path, trace: Path, workdir: Path) -> dict[str, tuple[str, int]]:
    """Source file and line Insights knows for each timer name (CPU profiler scopes only)."""
    out = workdir / "timers.csv"
    run_commands(insights, trace, [f"TimingInsights.ExportTimers {out}"], workdir)
    sources = {}
    if out.exists():
        with open(out, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("File"):
                    sources.setdefault(row["Name"], (row["File"], int(row.get("Line") or 0)))
    return sources


def exclusive_ms(events: list[Event], windows: list[FrameWindow | None], index: int) -> dict[tuple[str, str], float]:
    """Exclusive time in ms per (thread group, timer) for one frame.

    The game thread counts only the frame's own tick; render, RHI and GPU count
    the tick and the one after it, as in `window_for`. Event times are clipped
    to that interval, and each event's exclusive time is its clipped duration
    minus that of its direct children.
    """
    interval = window_for(windows, index)
    own = windows[index]
    if interval is None or own is None:
        return {}

    by_thread: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if thread_group(e.thread):
            by_thread[e.thread].append(e)

    totals: dict[tuple[str, str], float] = defaultdict(float)
    for thread, thread_events in by_thread.items():
        group = thread_group(thread)
        lo, hi = (own.start_s, own.end_s) if group == "game" else interval
        thread_events.sort(key=lambda e: (e.start_s, e.depth))

        stack: list[tuple[Event, float]] = []  # (event, child time so far)

        def close(item: tuple[Event, float]) -> None:
            event, child = item
            clipped = max(0.0, min(event.end_s, hi) - max(event.start_s, lo))
            totals[(group, event.timer)] += max(0.0, clipped - child) * 1000.0
            if stack:
                parent, parent_child = stack[-1]
                stack[-1] = (parent, parent_child + clipped)

        for e in thread_events:
            while stack and stack[-1][0].depth >= e.depth:
                close(stack.pop())
            stack.append((e, 0.0))
        while stack:
            close(stack.pop())

    return dict(totals)
