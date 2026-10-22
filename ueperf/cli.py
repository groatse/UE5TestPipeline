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
from ueperf.insights import InsightsError, find_insights


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
        print(f"  frames {h.start_frame}-{h.end_frame}  {h.start_ms / 1000:.2f} s  "
              f"peak {h.peak_ms:.1f} ms vs {h.baseline_ms:.1f} ms  stall {h.stall_ms:.1f} ms  {h.bound}")


def _print_causes(result: Analysis, report: CauseReport) -> None:
    print(f"\nTrace: {report.frames_read} of {report.frames_over} gameplay over-budget frames read "
          f"({report.startup_frames_over} more during startup), {report.baseline_frames} baseline frames, "
          f"trace clock +{report.trace_offset_s:.3f} s")

    def line(c) -> str:
        return f"{c.thread:<7} {c.timer[:70]:<70} +{c.rise_ms:.2f} ms (median {c.baseline_ms:.2f})"

    for h, causes, waits, window in zip(result.hitches, report.hitch_causes, report.hitch_waits, report.hitch_windows):
        at = f", trace {window[0]:.3f} s" if window else ""
        print(f"\n  Hitch at frame {h.start_frame} ({h.peak_ms:.1f} ms, {h.bound}-bound{at}):")
        for c in causes or []:
            print(f"    {line(c)}")
        if not causes:
            print("    no work timer rose above its baseline")
        for w in waits:
            print(f"    waiting: {line(w)}")
    for o, causes in zip(result.overruns, report.overrun_causes):
        print(f"\n  Overrun frames {o.start_frame}-{o.end_frame}, per frame:")
        for c in causes:
            print(f"    {line(c)}")

    print("\nGains ranking (over-budget time each timer accounts for):")
    for n, g in enumerate(report.gains[:15], 1):
        where = ", ".join(f"{r.start_s:.3f} s" for r in g.worst)
        print(f"  {n:>2}. {g.thread:<7} {g.timer[:70]:<70} {g.credit_ms:8.2f} ms  {g.frames} frames  worst at {where}")

    if report.hitch_groups:
        print("\nHitches by cause:")
        for g in report.hitch_groups:
            print(f"  {g.thread:<7} {g.timer[:70]:<70} {g.hitches} hitches, {g.stall_ms:.1f} ms stall")


def cmd_analyze(args: argparse.Namespace) -> int:
    # Absolute, because Unreal Insights resolves relative paths against its own folder.
    run = Path(args.run).resolve()
    frames_path = run / "frames.csv"
    if not frames_path.exists():
        print(f"No frames.csv in {run}", file=sys.stderr)
        return 1

    thresholds = load_thresholds(args.thresholds, budget_ms=args.budget_ms)
    frames = load_frames(frames_path)
    result = analyze(frames, thresholds)
    output = result.to_dict()
    _print_analysis(result)

    trace = run / "capture.utrace"
    if args.no_trace:
        pass
    elif not trace.exists():
        print(f"\nNo capture.utrace in {run}; skipping cause finding.")
    else:
        try:
            insights = find_insights(args.insights)
            print(f"\nReading the trace with {insights} ...")
            report = find_causes(frames, result, trace, insights, run / "trace_export", thresholds)
        except InsightsError as e:
            print(f"\nCause finding failed: {e}", file=sys.stderr)
            return 1
        output["causes"] = report.to_dict()
        _print_causes(result, report)

    out = run / "analysis.json"
    out.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
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
    p.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
