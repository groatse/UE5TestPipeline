"""Finds the call paths behind each hitch and overrun, and ranks the gains.

Every flagged frame is read from the trace as call paths: the chain of scopes
from a thread's top-level scope down to each scope, with inclusive and
exclusive time. Each path is compared with its median on in-budget baseline
frames. A hitch is explained by the call tree of the paths that rose, and by
its hot spots: the paths whose own (exclusive) time rose, which is where the
extra time was actually spent rather than passed through.

When a bound thread's rise is waiting (WaitForTasks and the like), the work
it waited for ran on task graph workers, so worker paths that rose are hot
spots too, scaled so they never claim more than the wait.
"""

from __future__ import annotations

import fnmatch
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path as FsPath

from ueperf.analyze import Analysis, frame_cost
from ueperf.config import Thresholds
from ueperf.ingest.frames import Frame
from ueperf.ingest.trace import (
    Path,
    align_frames,
    export_frame_events,
    export_ticks,
    export_timers,
    path_times,
    window_for,
)
from ueperf.source_index import SourceIndex, _enclosing_function

PathKey = tuple[str, Path]  # (thread group, call path)

OBJECT_RE = re.compile(r"^(Default__)?[A-Za-z]\w*_C(_\d+)?$")
MAX_TREE_NODES = 80
MAX_CHILDREN = 6


@dataclass(frozen=True)
class Scope:
    """What a scope name is and where its code is."""
    kind: str        # "cpp", "stat", "object", "blueprint", "gpu", "unknown"
    source: str      # "File.cpp:123 in Class::Function", or "" when unknown
    more_sites: int  # further places the same scope name is used


@dataclass(frozen=True)
class TreeNode:
    thread: str
    path: list[str]
    depth: int
    ms: float
    baseline_ms: float
    rise_ms: float
    self_rise_ms: float
    scope: Scope


@dataclass(frozen=True)
class HotSpot:
    thread: str
    path: list[str]
    self_ms: float
    baseline_ms: float
    rise_ms: float
    scope: Scope
    callers: list[str] = field(default_factory=list)  # path above it, framework scopes trimmed


@dataclass(frozen=True)
class Wait:
    thread: str
    timer: str
    rise_ms: float


@dataclass
class Explanation:
    """Why one frame (or a hitch's frames together) cost more than usual."""
    tree: list[TreeNode] = field(default_factory=list)
    hotspots: list[HotSpot] = field(default_factory=list)
    waits: list[Wait] = field(default_factory=list)
    trace_bound: str = ""  # thread group whose own work rose most, from the trace
    # Share of the stall the hot spots account for. The call tree is kept only when
    # it is low, i.e. when the time is spread over many scopes.
    hotspot_share: float = 0.0


@dataclass(frozen=True)
class TimeRange:
    frame: int
    start_s: float
    end_s: float
    credit_ms: float


@dataclass(frozen=True)
class Gain:
    thread: str
    path: list[str]
    scope: Scope
    credit_ms: float
    frames: int
    worst: list[TimeRange]
    callers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HitchGroup:
    thread: str
    path: list[str]
    hitches: int
    stall_ms: float


@dataclass
class CauseReport:
    trace_offset_s: float
    frames_read: int
    frames_over: int
    non_gameplay_frames_over: int
    skip_start_s: float
    baseline_frames: int
    hitch_windows: list[tuple[float, float] | None] = field(default_factory=list)
    hitches: list[Explanation] = field(default_factory=list)
    overruns: list[Explanation] = field(default_factory=list)
    gains: list[Gain] = field(default_factory=list)
    hitch_groups: list[HitchGroup] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- scope naming

def content_assets(project_dir: FsPath | None) -> dict[str, str]:
    """Asset name -> path under Content, for naming Blueprint classes and widgets."""
    assets: dict[str, str] = {}
    if project_dir is None:
        return assets
    content = project_dir / "Content"
    if content.exists():
        for f in content.rglob("*.uasset"):
            assets.setdefault(f.stem, str(f.relative_to(project_dir)).replace("\\", "/"))
    return assets


def _owner_blueprint(path: Path) -> str | None:
    """Nearest Blueprint above the last scope of `path`: an event graph or a *_C object."""
    for name in reversed(path[:-1]):
        if name.startswith("ExecuteUbergraph_"):
            return name[len("ExecuteUbergraph_"):]
        if OBJECT_RE.match(name):
            return re.sub(r"(_C)(_\d+)?$", "", name.replace("Default__", ""))
        if " /" in name:
            return name.split(" ", 1)[0]
    return None


class ScopeResolver:
    """Names what each scope is, from Insights' timer sources, the source index and project assets."""

    def __init__(self, index: SourceIndex | None, timer_sources: dict[str, tuple[str, int]], roots: list[FsPath],
                 assets: dict[str, str] | None = None):
        self.index = index
        self.timer_sources = timer_sources
        self.roots = [str(r).replace("\\", "/").rstrip("/") + "/" for r in roots]
        self.assets = assets or {}
        self.cache: dict[tuple[str, Path], Scope] = {}
        self.file_lines: dict[FsPath, list[str]] = {}

    def short(self, file: str) -> str:
        f = file.replace("\\", "/")
        for root in self.roots:
            if f.lower().startswith(root.lower()):
                return f[len(root):]
        # Insights records build-machine paths for engine code; keep from Engine/ on.
        i = f.find("/Engine/")
        return f[i + 1:] if i >= 0 else f

    def function_at(self, file: str, line: int) -> str:
        """Enclosing function of a source line, read from the local copy of the file."""
        rel = self.short(file)
        for root in self.roots:
            local = FsPath(root) / rel
            if local.exists():
                if local not in self.file_lines:
                    self.file_lines[local] = local.read_text(encoding="utf-8", errors="replace").splitlines()
                lines = self.file_lines[local]
                return _enclosing_function(lines, min(line, len(lines)) - 1) if lines else ""
        return ""

    def asset(self, class_name: str) -> str:
        return self.assets.get(class_name, "")

    def resolve(self, thread: str, path: Path, graph_below: str | None = None) -> Scope:
        """`graph_below` is the Blueprint whose event graph this scope calls into, if any."""
        key = (thread, path, graph_below or "")
        if key not in self.cache:
            self.cache[key] = self._resolve(thread, path, graph_below)
        return self.cache[key]

    def _resolve(self, thread: str, path: Path, graph_below: str | None) -> Scope:
        name = path[-1]
        if name in self.timer_sources:
            file, line = self.timer_sources[name]
            func = self.function_at(file, line)
            return Scope("cpp", f"{self.short(file)}:{line}" + (f" in {func}" if func else ""), 0)
        sites = self.index.sites(name) if self.index else []
        if sites:
            s = sites[0]
            label = f"{self.short(s.file)}:{s.line}" + (f" in {s.function}" if s.function else "")
            return Scope("gpu" if thread == "gpu" else "stat", label, len(sites) - 1)
        if name.endswith("_INST"):
            widget = next((p for p in reversed(path[:-1])
                           if p.startswith("ExecuteUbergraph_W") or re.match(r"^W\w*_C\b", p)), None)
            if widget:
                widget = re.sub(r"(_C)(_\d+)?$", "", widget.split(" ", 1)[0].replace("ExecuteUbergraph_", ""))
            where = self.asset(widget) if widget else ""
            label = f"widget animation {name[:-5]}" + (f" in {widget}" if widget else "") + (f" ({where})" if where else "")
            return Scope("object", label, 0)
        if name.startswith("ExecuteUbergraph_"):
            bp = name[len("ExecuteUbergraph_"):]
            return Scope("blueprint", f"event graph of {bp}" + (f" ({self.asset(bp)})" if self.asset(bp) else ""), 0)
        if " /" in name or OBJECT_RE.match(name):
            cls = re.sub(r"(_C)(_\d+)?$", "", name.split(" ", 1)[0].replace("Default__", ""))
            instance = name.split(" ", 1)[1] if " /" in name else ""
            where = self.asset(cls)
            parts = [p for p in (f"Blueprint {cls}" + (f" ({where})" if where else ""), instance) if p]
            return Scope("object", ", ".join(parts), 0)
        # A Blueprint function or event: called from Blueprint, or calling into an event graph.
        parent = path[-2] if len(path) > 1 else ""
        called_from_bp = (parent == "Blueprint Time" or parent.startswith("ExecuteUbergraph_")
                          or bool(OBJECT_RE.match(parent)) or " /" in parent)
        if re.match(r"^\w+$", name) and (graph_below or called_from_bp):
            owner = graph_below or _owner_blueprint(path)
            where = self.asset(owner) if owner else ""
            label = "Blueprint function or event" + (f" in {owner}" if owner else "") + (f" ({where})" if where else "")
            return Scope("blueprint", label, 0)
        if re.match(r"^[A-Z]\w+$", name):
            return Scope("uobject", "UObject scope (class or instance name)", 0)
        return Scope("gpu" if thread == "gpu" else "unknown", "", 0)


# ---------------------------------------------------------------- per-frame math

def is_ignored(timer: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(timer, p) for p in patterns)


def trim_callers(path: Path, patterns: tuple[str, ...]) -> list[str]:
    """The scopes above the last one, without the framework scopes every frame passes through."""
    return [name for name in path[:-1] if not is_ignored(name, patterns)]


def evenly(items: list[int], count: int) -> list[int]:
    """Up to `count` items spread evenly through `items`."""
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def baseline_medians(per_frame: dict[int, dict[PathKey, list[float]]], baseline: list[int]) -> dict[PathKey, tuple[float, float]]:
    """Median (inclusive, exclusive) ms per path over the baseline frames, absence counted as 0."""
    present = [i for i in baseline if i in per_frame]
    keys = {k for i in present for k in per_frame[i]}
    out = {}
    for k in keys:
        incl = [per_frame[i][k][0] if k in per_frame[i] else 0.0 for i in present]
        excl = [per_frame[i][k][1] if k in per_frame[i] else 0.0 for i in present]
        out[k] = (statistics.median(incl), statistics.median(excl))
    return out


def _sum_frames(frames_paths: list[dict[PathKey, list[float]]]) -> dict[PathKey, list[float]]:
    total: dict[PathKey, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for paths in frames_paths:
        for k, (incl, excl) in paths.items():
            total[k][0] += incl
            total[k][1] += excl
    return total


CPU_GROUPS = ("game", "render", "rhi")
SEARCHED_GROUPS = CPU_GROUPS + ("gpu",)


def explain(
    paths: dict[PathKey, list[float]],
    medians: dict[PathKey, tuple[float, float]],
    n_frames: int,
    resolver: ScopeResolver,
    t: Thresholds,
    include_gpu: bool = True,
) -> Explanation:
    """Call tree, hot spots and waits for one frame, or `n_frames` frames summed in `paths`.

    Every thread group is searched, not only the one the frame log calls bound:
    the frame log's thread times can land a frame late, so a game thread stall
    may show up there as a render-bound frame whose render thread only waited.
    """
    def base(k: PathKey) -> tuple[float, float]:
        b = medians.get(k, (0.0, 0.0))
        return b[0] * n_frames, b[1] * n_frames

    # Which Blueprint event graph each path calls straight into, to name a function's owner.
    graph_below: dict[PathKey, str] = {}
    for group, path in paths:
        if len(path) > 1 and path[-1].startswith("ExecuteUbergraph_"):
            graph_below[(group, path[:-1])] = path[-1][len("ExecuteUbergraph_"):]

    def scope_of(k: PathKey) -> Scope:
        return resolver.resolve(k[0], k[1], graph_below.get(k))

    # Waits on the CPU threads decide whether workers are searched too.
    waits: dict[tuple[str, str], float] = defaultdict(float)
    for k, (incl, excl) in paths.items():
        group, path = k
        if group in CPU_GROUPS and is_ignored(path[-1], t.causes_ignore):
            waits[(group, path[-1])] += excl - base(k)[1]
    wait_rise = sum(v for v in waits.values() if v > 0)
    search = [g for g in SEARCHED_GROUPS if include_gpu or g != "gpu"]
    search += ["workers"] if wait_rise >= t.causes_min_rise_ms else []

    # Hot spots: paths whose own time rose.
    hot: list[HotSpot] = []
    for k, (incl, excl) in paths.items():
        group, path = k
        if group not in search or is_ignored(path[-1], t.causes_ignore):
            continue
        b_incl, b_excl = base(k)
        self_rise = excl - b_excl
        if self_rise >= t.causes_min_rise_ms:
            hot.append(HotSpot(group, list(path), round(excl, 3), round(b_excl, 3), round(self_rise, 3), scope_of(k),
                               trim_callers(path, t.causes_trim)))
    # Workers run in parallel: never let them claim more than the waits they caused.
    worker_rise = sum(h.rise_ms for h in hot if h.thread == "workers")
    if worker_rise > wait_rise > 0:
        f = wait_rise / worker_rise
        hot = [HotSpot(h.thread, h.path, h.self_ms, h.baseline_ms, round(h.rise_ms * f, 3), h.scope, h.callers)
               if h.thread == "workers" else h for h in hot]
    hot.sort(key=lambda h: h.rise_ms, reverse=True)

    # Where the extra work was: the group with the largest own-time rise of real work.
    work = defaultdict(float)
    for h in hot:
        work[h.thread] += h.rise_ms
    trace_bound = max(work, key=work.get) if work else ""

    # Call tree of paths whose inclusive time rose enough to matter, busiest group first.
    total_rise = sum(h.rise_ms for h in hot)
    node_min = max(0.25 * n_frames, 0.03 * total_rise)
    rises: dict[PathKey, float] = {}
    for k, (incl, excl) in paths.items():
        if k[0] in search:
            rises[k] = incl - base(k)[0]
    children: dict[tuple[str, Path], list[PathKey]] = defaultdict(list)
    for k, r in rises.items():
        if r >= node_min:
            children[(k[0], k[1][:-1])].append(k)

    tree: list[TreeNode] = []

    def walk(group: str, prefix: Path, depth: int) -> None:
        kids = sorted(children.get((group, prefix), []), key=lambda k: rises[k], reverse=True)[:MAX_CHILDREN]
        for k in kids:
            if len(tree) >= MAX_TREE_NODES:
                return
            incl, excl = paths[k]
            b_incl, b_excl = base(k)
            scope = (Scope("wait", "waiting, not working", 0) if is_ignored(k[1][-1], t.causes_ignore)
                     else scope_of(k))
            tree.append(TreeNode(group, list(k[1]), depth, round(incl, 3), round(b_incl, 3), round(rises[k], 3),
                                 round(excl - b_excl, 3), scope))
            walk(group, k[1], depth + 1)

    for group in sorted(search, key=lambda g: work.get(g, 0.0), reverse=True):
        walk(group, (), 0)

    wait_list = [Wait(g, name, round(r, 3)) for (g, name), r in waits.items() if r >= t.causes_min_rise_ms]
    wait_list.sort(key=lambda w: w.rise_ms, reverse=True)
    return Explanation(tree=tree, hotspots=hot[:t.causes_top * 2], waits=wait_list[:3], trace_bound=trace_bound)


# ---------------------------------------------------------------- report

def compute_report(
    frames: list[Frame],
    analysis: Analysis,
    per_frame: dict[int, dict[PathKey, list[float]]],
    baseline: list[int],
    over_read: list[int],
    windows: list,
    resolver: ScopeResolver,
    t: Thresholds,
) -> CauseReport:
    medians = baseline_medians(per_frame, baseline)
    index_of = {f.frame: i for i, f in enumerate(frames)}
    flagged = [i for i in per_frame if i not in baseline]

    def frames_in(start_frame: int, end_frame: int) -> list[int]:
        lo, hi = index_of[start_frame], index_of[end_frame]
        return [i for i in range(lo, hi + 1) if i in per_frame and i not in baseline]

    # GPU scopes are read over wall-clock windows, so a long CPU stall makes them look
    # as if they rose. They are searched only when the frame log's GPU time itself rose.
    gpu_base = statistics.median(frames[i].gpu_ms for i in baseline) if baseline else 0.0

    def gpu_rose(idx: list[int]) -> bool:
        return any(frames[i].gpu_ms - gpu_base >= 1.0 for i in idx)

    def explain_frames(idx: list[int], average: bool) -> Explanation:
        if not idx:
            return Explanation()
        summed = _sum_frames([per_frame[i] for i in idx])
        n = len(idx)
        if average:
            summed = {k: [v[0] / n, v[1] / n] for k, v in summed.items()}
            n = 1
        return explain(summed, medians, n, resolver, t, include_gpu=gpu_rose(idx))

    def keep_tree_if_spread(expl: Explanation, stall_ms: float) -> Explanation:
        # When a few hot spots account for the stall, they are the answer and the
        # framework scopes above them add nothing. The tree only helps when the time
        # is spread thin over many scopes, so it is kept just for that case.
        explained = sum(h.rise_ms for h in expl.hotspots)
        expl.hotspot_share = round(min(1.0, explained / stall_ms), 3) if stall_ms > 0 else 0.0
        if expl.hotspot_share >= t.causes_tree_below_share:
            expl.tree = []
        return expl

    hitch_expl = [keep_tree_if_spread(explain_frames(frames_in(h.start_frame, h.end_frame), average=False), h.stall_ms)
                  for h in analysis.hitches]
    overrun_expl = [keep_tree_if_spread(explain_frames(frames_in(o.start_frame, o.end_frame), average=True),
                                        o.total_excess_ms / max(o.frames_over, 1))
                    for o in analysis.overruns]
    hitch_windows = [window_for(windows, index_of[h.start_frame]) for h in analysis.hitches]

    # Gains: hot-spot rise on each gameplay over-budget frame, scaled so a frame never
    # gives out more credit than its excess, summed per path, scaled to all such frames.
    gameplay = [analysis.is_gameplay(i, frames, t) for i in range(len(frames))]
    gameplay_over = [i for i, f in enumerate(frames) if frame_cost(f) > t.budget_ms and gameplay[i]]
    startup_over = analysis.summary.frames_over - len(gameplay_over)
    read = [i for i in over_read if i in per_frame and i not in baseline and gameplay[i]]
    scale = len(gameplay_over) / len(read) if read else 0.0

    credit: dict[PathKey, float] = defaultdict(float)
    scopes: dict[PathKey, Scope] = {}
    callers: dict[PathKey, list[str]] = {}
    hits: dict[PathKey, list[TimeRange]] = defaultdict(list)
    for i in read:
        excess = frame_cost(frames[i]) - t.budget_ms
        expl = explain(per_frame[i], medians, 1, resolver, t, include_gpu=gpu_rose([i]))
        total = sum(h.rise_ms for h in expl.hotspots)
        f = min(1.0, excess / total) if total > 0 else 0.0
        interval = window_for(windows, i)
        for h in expl.hotspots:
            key = (h.thread, tuple(h.path))
            share = h.rise_ms * f
            credit[key] += share
            scopes[key] = h.scope
            callers[key] = h.callers
            if interval:
                hits[key].append(TimeRange(frames[i].frame, round(interval[0], 6), round(interval[1], 6), round(share, 3)))

    gains = [
        Gain(thread, list(path), scopes[(thread, path)], round(total * scale, 3), len(hits[(thread, path)]),
             sorted(hits[(thread, path)], key=lambda r: r.credit_ms, reverse=True)[:3], callers[(thread, path)])
        for (thread, path), total in credit.items()
    ]
    gains.sort(key=lambda g: g.credit_ms, reverse=True)

    groups: dict[tuple[str, Path], list[float]] = defaultdict(lambda: [0, 0.0])
    for h, expl in zip(analysis.hitches, hitch_expl):
        top = expl.hotspots[0] if expl.hotspots else None
        key = (top.thread, tuple(top.path)) if top else ("unknown", ("unknown",))
        groups[key][0] += 1
        groups[key][1] += h.stall_ms
    hitch_groups = [HitchGroup(thread, list(path), int(n), round(stall, 3)) for (thread, path), (n, stall) in groups.items()]
    hitch_groups.sort(key=lambda g: g.stall_ms, reverse=True)

    aligned = [(w.start_s - f.t_ms / 1000.0) for f, w in zip(frames, windows) if w is not None]
    return CauseReport(
        trace_offset_s=round(statistics.median(aligned), 6) if aligned else 0.0,
        frames_read=len(read),
        frames_over=len(gameplay_over),
        non_gameplay_frames_over=startup_over,
        skip_start_s=t.causes_skip_start_s,
        baseline_frames=len([i for i in baseline if i in per_frame]),
        hitch_windows=hitch_windows,
        hitches=hitch_expl,
        overruns=overrun_expl,
        gains=gains[:25],
        hitch_groups=hitch_groups,
    )


def select_frames(frames: list[Frame], analysis: Analysis, t: Thresholds) -> tuple[list[int], list[int]]:
    """Which frames to read from the trace: (over budget, baseline)."""
    index_of = {f.frame: i for i, f in enumerate(frames)}
    over = [i for i, f in enumerate(frames) if frame_cost(f) > t.budget_ms]
    hitch = {i for h in analysis.hitches for i in range(index_of[h.start_frame], index_of[h.end_frame] + 1)}
    over_read = sorted(set(evenly(over, t.causes_max_frames)) | hitch)

    in_budget = [i for i, f in enumerate(frames) if frame_cost(f) <= t.budget_ms and analysis.is_gameplay(i, frames, t)]
    # Keep away from the last frame, whose following tick may be missing.
    baseline = evenly([i for i in in_budget if i < len(frames) - 1], t.causes_baseline_frames)
    return over_read, baseline


def find_causes(
    frames: list[Frame],
    analysis: Analysis,
    trace: FsPath,
    insights: FsPath,
    workdir: FsPath,
    t: Thresholds,
    index: SourceIndex | None = None,
    roots: list[FsPath] | None = None,
    project_dir: FsPath | None = None,
    windows: list | None = None,
) -> CauseReport:
    """Read the trace for the flagged and baseline frames and build the cause report.

    `windows` is each frame's tick on the trace clock, from `align_frames`; it is
    exported and aligned here when not given.
    """
    if windows is None:
        windows = align_frames(frames, export_ticks(insights, trace, workdir))
    timer_sources = export_timers(insights, trace, workdir)
    over_read, baseline = select_frames(frames, analysis, t)
    events = export_frame_events(insights, trace, windows, sorted(set(over_read) | set(baseline)), workdir)
    per_frame = {i: path_times(evs, windows, i) for i, evs in events.items()}
    resolver = ScopeResolver(index, timer_sources, roots or [], content_assets(project_dir))
    return compute_report(frames, analysis, per_frame, baseline, over_read, windows, resolver, t)
