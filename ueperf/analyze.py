"""Finds over-budget frames, sustained overruns and hitches in a run's frames."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field, replace

from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.markers import Marker
from ueperf.phases import NON_GAMEPLAY, assign_phases, last_marker_before

MIXED = "mixed"


@dataclass(frozen=True)
class Overrun:
    start_frame: int
    end_frame: int
    start_ms: float
    end_ms: float
    frames: int
    frames_over: int
    total_excess_ms: float
    bound: str
    peak_frame: int
    peak_ms: float
    phase: str = ""
    after_marker: str = ""
    after_marker_s: float = 0.0


@dataclass(frozen=True)
class Hitch:
    start_frame: int
    end_frame: int
    start_ms: float
    end_ms: float
    frames: int
    peak_ms: float
    baseline_ms: float
    stall_ms: float
    bound: str
    phase: str = ""
    after_marker: str = ""
    after_marker_s: float = 0.0


@dataclass(frozen=True)
class PhaseSummary:
    phase: str
    start_frame: int
    start_ms: float
    frames: int
    duration_s: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    frames_over: int


@dataclass(frozen=True)
class Summary:
    frames: int
    duration_s: float
    budget_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    frames_over: int
    share_over: float
    bound_counts: dict[str, int]


@dataclass
class Analysis:
    summary: Summary
    overruns: list[Overrun] = field(default_factory=list)
    hitches: list[Hitch] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    phases: list[PhaseSummary] = field(default_factory=list)
    frame_phases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("frame_phases")  # one entry per frame; `phases` carries the same information
        return d

    def is_gameplay(self, index: int, frames: list[Frame], t: Thresholds) -> bool:
        """Whether frame `index` is gameplay, not startup or loading.

        Uses lifecycle phases when the run has markers, else treats the first
        `causes_skip_start_s` seconds as startup.
        """
        if self.frame_phases:
            return self.frame_phases[index] not in NON_GAMEPLAY
        return frames[index].t_ms >= frames[0].t_ms + t.causes_skip_start_s * 1000.0


def frame_cost(frame: Frame) -> float:
    """What the frame cost the player: its duration plus compositor GPU time."""
    return frame.frame_ms + frame.compositor_gpu_ms


def bound_type(frame: Frame, mixed_within: float) -> str:
    """The thread or GPU with the largest time, or "mixed" when the top two are close."""
    ranked = sorted(frame.threads().items(), key=lambda kv: kv[1], reverse=True)
    (top_name, top_ms), (_, second_ms) = ranked[0], ranked[1]
    if top_ms > 0 and second_ms >= top_ms * (1.0 - mixed_within):
        return MIXED
    return top_name


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    if not sorted_values:
        return 0.0
    rank = max(1, round(pct / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def _majority_bound(frames: list[Frame], t: Thresholds) -> str:
    counts = Counter(bound_type(f, t.mixed_within) for f in frames)
    return counts.most_common(1)[0][0] if counts else MIXED


def find_overruns(frames: list[Frame], t: Thresholds) -> list[Overrun]:
    """Stretches of over-budget frames with short in-budget gaps, long enough to be sustained."""
    over = [i for i, f in enumerate(frames) if frame_cost(f) > t.budget_ms]
    groups: list[list[int]] = []
    for i in over:
        # A gap of N in-budget frames between two over-budget frames means i - prev - 1 == N.
        if groups and i - groups[-1][-1] - 1 <= t.overrun_max_gap_frames:
            groups[-1].append(i)
        else:
            groups.append([i])

    overruns = []
    for group in groups:
        first, last = group[0], group[-1]
        span = last - first + 1
        if span < t.overrun_min_frames:
            continue
        over_frames = [frames[i] for i in group]
        peak = max(over_frames, key=frame_cost)
        overruns.append(Overrun(
            start_frame=frames[first].frame,
            end_frame=frames[last].frame,
            start_ms=frames[first].t_ms,
            end_ms=frames[last].t_ms,
            frames=span,
            frames_over=len(group),
            total_excess_ms=round(sum(frame_cost(f) - t.budget_ms for f in over_frames), 3),
            bound=_majority_bound(over_frames, t),
            peak_frame=peak.frame,
            peak_ms=frame_cost(peak),
        ))
    return overruns


def find_hitches(frames: list[Frame], t: Thresholds) -> list[Hitch]:
    """Short runs of frames far above the rolling median of the frames before them."""
    flagged: list[tuple[int, float]] = []  # (index, baseline median)
    for i, f in enumerate(frames):
        window = frames[max(0, i - t.hitch_window_frames):i]
        if len(window) < t.hitch_min_window_frames:
            continue
        baseline = statistics.median(frame_cost(w) for w in window)
        cost = frame_cost(f)
        if cost <= t.hitch_factor * baseline:
            continue
        if t.hitch_require_over_budget and cost <= t.budget_ms:
            continue
        flagged.append((i, baseline))

    runs: list[list[tuple[int, float]]] = []
    for item in flagged:
        if runs and item[0] == runs[-1][-1][0] + 1:
            runs[-1].append(item)
        else:
            runs.append([item])

    hitches = []
    for run in runs:
        if len(run) > t.hitch_max_frames:
            continue
        # The first frame's baseline is untouched by the hitch itself.
        baseline = run[0][1]
        run_frames = [frames[i] for i, _ in run]
        peak = max(run_frames, key=frame_cost)
        hitches.append(Hitch(
            start_frame=run_frames[0].frame,
            end_frame=run_frames[-1].frame,
            start_ms=run_frames[0].t_ms,
            end_ms=run_frames[-1].t_ms,
            frames=len(run_frames),
            peak_ms=frame_cost(peak),
            baseline_ms=round(baseline, 3),
            stall_ms=round(sum(frame_cost(f) - baseline for f in run_frames), 3),
            bound=bound_type(peak, t.mixed_within),
        ))
    return hitches


def summarize(frames: list[Frame], t: Thresholds) -> Summary:
    costs = sorted(frame_cost(f) for f in frames)
    over = [f for f in frames if frame_cost(f) > t.budget_ms]
    duration_s = (frames[-1].t_ms - frames[0].t_ms) / 1000.0 if len(frames) > 1 else 0.0
    return Summary(
        frames=len(frames),
        duration_s=round(duration_s, 3),
        budget_ms=t.budget_ms,
        median_ms=round(statistics.median(costs), 3) if costs else 0.0,
        p95_ms=percentile(costs, 95),
        p99_ms=percentile(costs, 99),
        max_ms=costs[-1] if costs else 0.0,
        frames_over=len(over),
        share_over=round(len(over) / len(frames), 4) if frames else 0.0,
        bound_counts=dict(Counter(bound_type(f, t.mixed_within) for f in over)),
    )


def summarize_phases(frames: list[Frame], frame_phases: list[str], t: Thresholds) -> list[PhaseSummary]:
    """One summary per contiguous stretch of the same phase, in order."""
    out, start = [], 0
    for i in range(1, len(frames) + 1):
        if i < len(frames) and frame_phases[i] == frame_phases[start]:
            continue
        stretch = frames[start:i]
        costs = sorted(frame_cost(f) for f in stretch)
        out.append(PhaseSummary(
            phase=frame_phases[start],
            start_frame=stretch[0].frame,
            start_ms=stretch[0].t_ms,
            frames=len(stretch),
            duration_s=round((stretch[-1].t_ms - stretch[0].t_ms) / 1000.0, 3),
            median_ms=round(statistics.median(costs), 3),
            p95_ms=percentile(costs, 95),
            p99_ms=percentile(costs, 99),
            frames_over=sum(1 for c in costs if c > t.budget_ms),
        ))
        start = i
    return out


def _with_context(item, index_of: dict[int, int], frame_phases: list[str], markers: list[Marker]):
    """Attach the phase and the latest lifecycle marker before an overrun or hitch."""
    phase = frame_phases[index_of[item.start_frame]] if frame_phases else ""
    marker = last_marker_before(markers, item.start_frame)
    if marker is None:
        return replace(item, phase=phase)
    return replace(item, phase=phase, after_marker=marker.name,
                   after_marker_s=round((item.start_ms - marker.t_ms) / 1000.0, 3))


def analyze(
    frames: list[Frame],
    t: Thresholds,
    markers: list[Marker] | None = None,
    phase_map: dict[str, str] | None = None,
) -> Analysis:
    markers = sorted(markers or [], key=lambda m: (m.frame, m.t_ms))
    frame_phases = assign_phases(frames, markers, phase_map or {}) if markers else []
    index_of = {f.frame: i for i, f in enumerate(frames)}
    return Analysis(
        summary=summarize(frames, t),
        overruns=[_with_context(o, index_of, frame_phases, markers) for o in find_overruns(frames, t)],
        hitches=[_with_context(h, index_of, frame_phases, markers) for h in find_hitches(frames, t)],
        markers=markers,
        phases=summarize_phases(frames, frame_phases, t) if frame_phases else [],
        frame_phases=frame_phases,
    )
