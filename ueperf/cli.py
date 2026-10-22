"""Command line: ueperf analyze <run folder>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ueperf.analyze import Analysis, analyze
from ueperf.config import load_thresholds
from ueperf.ingest.frames import load_frames


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


def cmd_analyze(args: argparse.Namespace) -> int:
    run = Path(args.run)
    frames_path = run / "frames.csv"
    if not frames_path.exists():
        print(f"No frames.csv in {run}", file=sys.stderr)
        return 1

    thresholds = load_thresholds(args.thresholds, budget_ms=args.budget_ms)
    frames = load_frames(frames_path)
    result = analyze(frames, thresholds)

    out = run / "analysis.json"
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    _print_analysis(result)
    print(f"\nWrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ueperf")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="find overruns and hitches in a run folder")
    p.add_argument("run", help="run folder containing frames.csv")
    p.add_argument("--budget-ms", type=float, default=None, help="frame budget in ms (default 11.1)")
    p.add_argument("--thresholds", type=Path, default=None, help="thresholds TOML file")
    p.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
