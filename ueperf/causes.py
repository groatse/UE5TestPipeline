"""Names the trace timers behind each hitch and overrun, and ranks the gains.

For every over-budget frame read from the trace, each timer on the frame's
bound thread is compared with its median on in-budget baseline frames. The
time it is above that median is its rise. A timer's credit on a frame is its
rise capped at the frame's excess, and the gains ranking sums that credit
across the run.
"""

from __future__ import annotations

import fnmatch
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ueperf.analyze import MIXED, Analysis, bound_type, frame_cost
from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.trace import align_frames, exclusive_ms, export_frame_events, export_ticks, window_for

Key = tuple[str, str]  # (thread group, timer name)


@dataclass(frozen=True)
class Cause:
    thread: str
    timer: str
    ms: float
    baseline_ms: float
    rise_ms: float


@dataclass(frozen=True)
class TimeRange:
    frame: int
    start_s: float
    end_s: float
    credit_ms: float


@dataclass(frozen=True)
class Gain:
    thread: str
    timer: str
    credit_ms: float
    frames: int
    worst: list[TimeRange]


@dataclass(frozen=True)
class HitchGroup:
    thread: str
    timer: str
    hitches: int
    stall_ms: float


@dataclass
class CauseReport:
    trace_offset_s: float
    frames_read: int
    frames_over: int
    startup_frames_over: int
    skip_start_s: float
    baseline_frames: int
    hitch_windows: list[tuple[float, float] | None] = field(default_factory=list)
    hitch_causes: list[list[Cause]] = field(default_factory=list)
    hitch_waits: list[list[Cause]] = field(default_factory=list)
    overrun_causes: list[list[Cause]] = field(default_factory=list)
    gains: list[Gain] = field(default_factory=list)
    hitch_groups: list[HitchGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def is_ignored(timer: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(timer, p) for p in patterns)


def bound_groups(frame: Frame, t: Thresholds) -> list[str]:
    """Thread groups to search for causes: the bound one, or the top two when mixed."""
    bound = bound_type(frame, t.mixed_within)
    if bound != MIXED:
        return [bound]
    ranked = sorted(frame.threads().items(), key=lambda kv: kv[1], reverse=True)
    return [ranked[0][0], ranked[1][0]]


def evenly(items: list[int], count: int) -> list[int]:
    """Up to `count` items spread evenly through `items`."""
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def baseline_medians(per_frame: dict[int, dict[Key, float]], baseline: list[int]) -> dict[Key, float]:
    """Median exclusive time per key over the baseline frames, counting absence as 0."""
    keys = {k for i in baseline for k in per_frame.get(i, {})}
    present = [i for i in baseline if i in per_frame]
    if not present:
        return {}
    return {k: statistics.median(per_frame[i].get(k, 0.0) for i in present) for k in keys}


def frame_rises(
    frame: Frame,
    exclusive: dict[Key, float],
    medians: dict[Key, float],
    t: Thresholds,
) -> tuple[list[Cause], list[Cause]]:
    """Timers that rose above their baseline on one frame: (causes, waits), largest first.

    Causes are work timers on the frame's bound thread. When the bound thread's
    wait timers rose, the work it waited for ran on task graph workers, so
    worker timers that rose are causes too, each capped at the total wait rise.
    """
    groups = bound_groups(frame, t)
    causes, waits, workers = [], [], []
    for (group, timer), ms in exclusive.items():
        if group not in groups and group != "workers":
            continue
        base = medians.get((group, timer), 0.0)
        rise = ms - base
        if rise < t.causes_min_rise_ms:
            continue
        cause = Cause(group, timer, round(ms, 3), round(base, 3), round(rise, 3))
        if is_ignored(timer, t.causes_ignore):
            if group != "workers":
                waits.append(cause)
        elif group == "workers":
            workers.append(cause)
        else:
            causes.append(cause)

    wait_rise = sum(w.rise_ms for w in waits)
    if wait_rise >= t.causes_min_rise_ms:
        causes.extend(
            Cause(c.thread, c.timer, c.ms, c.baseline_ms, round(min(c.rise_ms, wait_rise), 3))
            for c in workers
        )
    causes.sort(key=lambda c: c.rise_ms, reverse=True)
    waits.sort(key=lambda c: c.rise_ms, reverse=True)
    return causes, waits


def _combine(cause_lists: list[list[Cause]], top: int, average: bool) -> list[Cause]:
    """Merge per-frame causes for a hitch (summed) or an overrun (averaged)."""
    sums: dict[Key, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for causes in cause_lists:
        for c in causes:
            s = sums[(c.thread, c.timer)]
            s[0] += c.ms
            s[1] += c.baseline_ms
            s[2] += c.rise_ms
    n = len(cause_lists) if average and cause_lists else 1
    merged = [
        Cause(thread, timer, round(ms / n, 3), round(base / n, 3), round(rise / n, 3))
        for (thread, timer), (ms, base, rise) in sums.items()
    ]
    merged.sort(key=lambda c: c.rise_ms, reverse=True)
    return merged[:top]


def compute_report(
    frames: list[Frame],
    analysis: Analysis,
    per_frame: dict[int, dict[Key, float]],
    baseline: list[int],
    over_read: list[int],
    windows: list,
    t: Thresholds,
) -> CauseReport:
    """Build the cause report from per-frame exclusive times already read from the trace."""
    medians = baseline_medians(per_frame, baseline)
    index_of = {f.frame: i for i, f in enumerate(frames)}
    rises = {i: frame_rises(frames[i], per_frame[i], medians, t) for i in per_frame if i not in baseline}

    def frames_in(start_frame: int, end_frame: int) -> list[int]:
        lo, hi = index_of[start_frame], index_of[end_frame]
        return [i for i in range(lo, hi + 1) if i in rises]

    hitch_frames = [frames_in(h.start_frame, h.end_frame) for h in analysis.hitches]
    hitch_causes = [_combine([rises[i][0] for i in idx], t.causes_top, average=False) for idx in hitch_frames]
    hitch_waits = [_combine([rises[i][1] for i in idx], 3, average=False) for idx in hitch_frames]
    hitch_windows = [window_for(windows, index_of[h.start_frame]) for h in analysis.hitches]
    overrun_causes = [_combine([rises[i][0] for i in frames_in(o.start_frame, o.end_frame)], t.causes_top, average=True)
                      for o in analysis.overruns]

    # Gains: credit per key over the gameplay over-budget frames read, scaled to
    # all gameplay over-budget frames. Startup frames are counted but left out.
    start_ms = frames[0].t_ms + t.causes_skip_start_s * 1000.0 if frames else 0.0
    gameplay_over = [i for i, f in enumerate(frames) if frame_cost(f) > t.budget_ms and f.t_ms >= start_ms]
    startup_over = analysis.summary.frames_over - len(gameplay_over)
    read = [i for i in over_read if i in rises and frames[i].t_ms >= start_ms]
    scale = len(gameplay_over) / len(read) if read else 0.0
    credit: dict[Key, float] = defaultdict(float)
    hits: dict[Key, list[TimeRange]] = defaultdict(list)
    for i in read:
        excess = frame_cost(frames[i]) - t.budget_ms
        interval = window_for(windows, i)
        for c in rises[i][0]:
            share = min(c.rise_ms, excess)
            key = (c.thread, c.timer)
            credit[key] += share
            if interval:
                hits[key].append(TimeRange(frames[i].frame, round(interval[0], 6), round(interval[1], 6), round(share, 3)))

    gains = [
        Gain(thread, timer, round(total * scale, 3), len(hits[(thread, timer)]),
             sorted(hits[(thread, timer)], key=lambda r: r.credit_ms, reverse=True)[:3])
        for (thread, timer), total in credit.items()
    ]
    gains.sort(key=lambda g: g.credit_ms, reverse=True)

    groups: dict[Key, list[float]] = defaultdict(lambda: [0, 0.0])
    for h, causes in zip(analysis.hitches, hitch_causes):
        key = (causes[0].thread, causes[0].timer) if causes else ("unknown", "unknown")
        groups[key][0] += 1
        groups[key][1] += h.stall_ms
    hitch_groups = [HitchGroup(thread, timer, int(n), round(stall, 3)) for (thread, timer), (n, stall) in groups.items()]
    hitch_groups.sort(key=lambda g: g.stall_ms, reverse=True)

    aligned = [(w.start_s - f.t_ms / 1000.0) for f, w in zip(frames, windows) if w is not None]
    return CauseReport(
        trace_offset_s=round(statistics.median(aligned), 6) if aligned else 0.0,
        frames_read=len(read),
        frames_over=len(gameplay_over),
        startup_frames_over=startup_over,
        skip_start_s=t.causes_skip_start_s,
        baseline_frames=len([i for i in baseline if i in per_frame]),
        hitch_windows=hitch_windows,
        hitch_causes=hitch_causes,
        hitch_waits=hitch_waits,
        overrun_causes=overrun_causes,
        gains=gains[:25],
        hitch_groups=hitch_groups,
    )


def select_frames(frames: list[Frame], analysis: Analysis, t: Thresholds) -> tuple[list[int], list[int]]:
    """Which frames to read from the trace: (over budget, baseline)."""
    index_of = {f.frame: i for i, f in enumerate(frames)}
    over = [i for i, f in enumerate(frames) if frame_cost(f) > t.budget_ms]
    hitch = {i for h in analysis.hitches for i in range(index_of[h.start_frame], index_of[h.end_frame] + 1)}
    over_read = sorted(set(evenly(over, t.causes_max_frames)) | hitch)

    start_ms = frames[0].t_ms + t.causes_skip_start_s * 1000.0 if frames else 0.0
    in_budget = [i for i, f in enumerate(frames) if frame_cost(f) <= t.budget_ms and f.t_ms >= start_ms]
    # Keep away from the last frame, whose following tick may be missing.
    baseline = evenly([i for i in in_budget if i < len(frames) - 1], t.causes_baseline_frames)
    return over_read, baseline


def find_causes(
    frames: list[Frame],
    analysis: Analysis,
    trace: Path,
    insights: Path,
    workdir: Path,
    t: Thresholds,
) -> CauseReport:
    """Read the trace for the flagged and baseline frames and build the cause report."""
    ticks = export_ticks(insights, trace, workdir)
    windows = align_frames(frames, ticks)
    over_read, baseline = select_frames(frames, analysis, t)
    events = export_frame_events(insights, trace, windows, sorted(set(over_read) | set(baseline)), workdir)
    per_frame = {i: exclusive_ms(evs, windows, i) for i, evs in events.items()}
    return compute_report(frames, analysis, per_frame, baseline, over_read, windows, t)
