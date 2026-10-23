"""Command line: ueperf analyze <run folder>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ueperf.analyze import Analysis, analyze
from ueperf.causes import CauseReport, find_causes
from ueperf.config import load_thresholds
from ueperf.ingest.frames import load_frames
from ueperf.ingest.markers import load_markers, markers_from_trace_times
from ueperf.ingest.trace import align_frames, export_ticks_and_scopes
from ueperf.insights import InsightsError, find_insights
from ueperf.loading import read_run_info
from ueperf.profile import DEFAULT_PHASES, load_profile
from ueperf.report import write_report
from ueperf.source_index import load_or_build


def _print_analysis(result: Analysis) -> None:
    s = result.summary
    print(f"Frames {s.frames} over {s.duration_s:.1f} s, budget {s.budget_ms:g} ms")
    print(f"Frame time: median {s.median_ms:.2f}  p95 {s.p95_ms:.2f}  p99 {s.p99_ms:.2f}  max {s.max_ms:.2f} ms")
    print(f"Over budget: {s.frames_over} frames ({s.share_over:.1%})", end="")
    if s.bound_counts:
        bounds = ", ".join(f"{k} {v}" for k, v in sorted(s.bound_counts.items(), key=lambda kv: -kv[1]))
        print(f"; bound by {bounds}")
    else:
        print()

    print(f"\nSustained overruns: {len(result.overruns)}")
    for o in result.overruns:
        print(f"  frames {o.start_frame}-{o.end_frame}  {o.start_ms / 1000:.2f}-{o.end_ms / 1000:.2f} s  "
              f"{o.frames_over}/{o.frames} over  excess {o.total_excess_ms:.1f} ms  "
              f"peak {o.peak_ms:.1f} ms  {o.bound}")

    print(f"\nHitches: {len(result.hitches)}")
    for h in result.hitches:
        context = f"  [{h.phase}, {h.after_marker_s:.2f} s after {h.after_marker}]" if h.after_marker else ""
        print(f"  frames {h.start_frame}-{h.end_frame}  {h.start_ms / 1000:.2f} s  "
              f"peak {h.peak_ms:.1f} ms vs {h.baseline_ms:.1f} ms  stall {h.stall_ms:.1f} ms  {h.bound}{context}")


KIND_LABEL = {"cpp": "C++", "stat": "C++ stat", "gpu": "GPU pass", "object": "object", "blueprint": "Blueprint",
              "uobject": "UObject", "wait": "wait", "unknown": "?"}


def _scope_text(scope) -> str:
    where = scope.source or "no source found"
    more = f" (+{scope.more_sites} more sites)" if scope.more_sites else ""
    return f"[{KIND_LABEL.get(scope.kind, scope.kind)}] {where}{more}"


def _print_explanation(expl, indent: str = "    ") -> None:
    if not expl.tree and not expl.hotspots:
        print(f"{indent}no scope rose above its baseline")
    for h in expl.hotspots[:8]:
        print(f"{indent}+{h.rise_ms:6.2f} ms  {h.scope.source or 'no source location'}")
        print(f"{indent}           scope {h.path[-1]} [{KIND_LABEL.get(h.scope.kind, h.scope.kind)}] on {h.thread}")
        if h.callers:
            print(f"{indent}           called from: {' > '.join(h.callers)}")
    for w in expl.waits:
        print(f"{indent}waiting: {w.thread} {w.timer} +{w.rise_ms:.2f} ms")
    if expl.tree:
        print(f"{indent}Hot spots explain only {expl.hotspot_share:.0%} of the stall; call tree of what rose:")
        for node in expl.tree:
            pad = "  " * node.depth
            self_note = f", self +{node.self_rise_ms:.2f}" if node.self_rise_ms >= 0.25 else ""
            print(f"{indent}  {node.thread:<7} {pad}{node.path[-1]}  {node.ms:.2f} ms (+{node.rise_ms:.2f}{self_note})"
                  f"  {_scope_text(node.scope)}")


def _print_causes(result: Analysis, report: CauseReport) -> None:
    print(f"\nTrace: {report.frames_read} of {report.frames_over} gameplay over-budget frames read "
          f"({report.non_gameplay_frames_over} more during startup or loading),{report.baseline_frames} baseline frames, "
          f"trace clock +{report.trace_offset_s:.3f} s")

    for n, (h, expl, window) in enumerate(zip(result.hitches, report.hitches, report.hitch_windows), 1):
        at = f", trace {window[0]:.3f} to {window[1]:.3f} s" if window else ""
        work = (f"; trace shows the extra work on {expl.trace_bound}"
                if expl.trace_bound and expl.trace_bound != h.bound else "")
        print(f"\nH{n}: frame {h.start_frame}, {h.peak_ms:.1f} ms vs {h.baseline_ms:.1f} ms, {h.bound}-bound{at}{work}")
        _print_explanation(expl)
    for n, (o, expl) in enumerate(zip(result.overruns, report.overruns), 1):
        print(f"\nO{n}: frames {o.start_frame}-{o.end_frame}, per frame on average")
        _print_explanation(expl)

    print("\nGains ranking (over-budget time each hot spot accounts for):")
    for n, g in enumerate(report.gains[:15], 1):
        where = ", ".join(f"{r.start_s:.3f} s" for r in g.worst)
        print(f"  {n:>2}. {g.credit_ms:8.2f} ms  {g.scope.source or 'no source location'}")
        print(f"      scope {g.path[-1]} on {g.thread}; {g.frames} frames, worst at {where}")
        if g.callers:
            print(f"      called from: {' > '.join(g.callers)}")


def _print_loading(result: Analysis) -> None:
    ld = result.loading
    if ld is None:
        return

    def s(v):
        return f"{v:.2f} s" if v is not None else "n/a"

    print(f"\nLoading (playable = first {ld.window_s:g} s window with at most {ld.max_share_over:.0%} of frames "
          f"over budget and none over {ld.max_frame_ratio:g}x budget):")
    print(f"  launch to playable      {s(ld.launch_to_playable_s)}")
    print(f"  launch to recording     {s(ld.launch_to_recording_s)}")
    print(f"  map load                {s(ld.map_load_s)}")
    print(f"  map loaded to playable  {s(ld.map_loaded_to_playable_s)}")
    if ld.playable_frame is None:
        print("  the frame rate never became steady in this run")


def _print_phases(result: Analysis) -> None:
    if not result.markers:
        return
    print("\nLifecycle markers:")
    for m in result.markers:
        src = " (from trace)" if m.source == "trace" else ""
        print(f"  {m.t_ms / 1000:7.2f} s  frame {m.frame:<6} {m.name}  {m.detail}{src}")
    print("\nPhases:")
    for p in result.phases:
        print(f"  {p.phase:<12} from {p.start_ms / 1000:6.2f} s  {p.frames:>6} frames  median {p.median_ms:.2f}  "
              f"p95 {p.p95_ms:.2f}  p99 {p.p99_ms:.2f} ms  {p.frames_over} over budget")


def cmd_analyze(args: argparse.Namespace) -> int:
    # Absolute, because Unreal Insights resolves relative paths against its own folder.
    run = Path(args.run).resolve()
    frames_path = run / "frames.csv"
    if not frames_path.exists():
        print(f"No frames.csv in {run}", file=sys.stderr)
        return 1

    profile = load_profile(args.profile) if args.profile else None
    budget = args.budget_ms if args.budget_ms is not None else (profile.budget_ms if profile else None)
    thresholds = load_thresholds(args.thresholds, budget_ms=budget)
    frames = load_frames(frames_path)
    markers = load_markers(run / "markers.csv")
    phase_map = profile.phases if profile else DEFAULT_PHASES
    project = Path(args.project).resolve() if args.project else (profile.project_dir if profile else None)

    trace = run / "capture.utrace"
    use_trace = not args.no_trace and trace.exists()
    windows, insights = None, None
    if use_trace:
        try:
            insights = find_insights(args.insights)
            scope_events = profile.scope_events() if profile else []
            print(f"Reading ticks{' and lifecycle scopes' if scope_events else ''} from the trace ...")
            ticks, scope_times = export_ticks_and_scopes(
                insights, trace, run / "trace_export", [e["scope"] for e in scope_events])
            windows = align_frames(frames, ticks)
            markers += markers_from_trace_times(
                scope_times, {e["scope"]: e["name"] for e in scope_events}, frames, windows,
                {e["scope"]: bool(e.get("repeat")) for e in scope_events})
        except InsightsError as e:
            print(f"\nReading the trace failed: {e}", file=sys.stderr)
            return 1

    result = analyze(frames, thresholds, markers, phase_map, read_run_info(run / "run_info.txt"))
    output = result.to_dict()
    _print_analysis(result)
    _print_loading(result)
    _print_phases(result)

    if args.no_trace:
        pass
    elif not use_trace:
        print(f"\nNo capture.utrace in {run}; skipping cause finding.")
    else:
        try:
            engine = insights.parents[3]
            index = load_or_build(engine, project, rebuild=args.reindex)
            roots = [r for r in (project, engine) if r]
            print(f"\nReading flagged frames from the trace with {insights} ...")
            report = find_causes(frames, result, trace, insights, run / "trace_export", thresholds, index, roots,
                                 project, windows)
        except InsightsError as e:
            print(f"\nCause finding failed: {e}", file=sys.stderr)
            return 1
        output["causes"] = report.to_dict()
        _print_causes(result, report)

    out = run / "analysis.json"
    out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    print(f"Wrote {write_report(run)}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    out = load_profile(args.profile).write_events_json(Path(args.out))
    print(f"Wrote {out}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    run = Path(args.run).resolve()
    if not (run / "analysis.json").exists():
        print(f"No analysis.json in {run}; run 'ueperf analyze' first", file=sys.stderr)
        return 1
    print(f"Wrote {write_report(run)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ueperf")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="find overruns and hitches in a run folder")
    p.add_argument("run", help="run folder containing frames.csv")
    p.add_argument("--budget-ms", type=float, default=None, help="frame budget in ms (default 11.1)")
    p.add_argument("--thresholds", type=Path, default=None, help="thresholds TOML file")
    p.add_argument("--no-trace", action="store_true", help="skip cause finding from capture.utrace")
    p.add_argument("--insights", type=Path, default=None, help="path to UnrealInsights.exe")
    p.add_argument("--project", type=Path, default=None,
                   help="project folder, so scopes in game code resolve to its source")
    p.add_argument("--reindex", action="store_true", help="rebuild the cached engine source index")
    p.add_argument("--profile", type=Path, default=None,
                   help="game profile: budget, project folder, lifecycle scopes and phases")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("events", help="write the plugin's -PerfEvents file from a game profile")
    p.add_argument("profile", type=Path, help="game profile TOML")
    p.add_argument("out", type=Path, help="JSON file to write")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("report", help="write report.html from a run folder's analysis.json")
    p.add_argument("run", help="run folder containing analysis.json and frames.csv")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
