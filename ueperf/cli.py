"""Command line: ueperf run, analyze, report, send, status and events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ueperf.analyze import Analysis, analyze
from ueperf.causes import CauseReport, find_causes
from ueperf.config import load_thresholds
from ueperf.ingest.frames import load_frames
from ueperf.ingest.inputs import load_coverage, load_inputs
from ueperf.ingest.markers import load_markers, markers_from_trace_times
from ueperf.ingest.trace import align_frames, export_ticks_and_scopes
from ueperf.insights import InsightsError, find_insights
from ueperf.launcher import latest_run, read_status, run_playtest, screenshot_times, send_command
from ueperf.loading import read_run_info
from ueperf.onboard import draft_profile
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
        if h.inputs:
            print(f"    input: {'; '.join(h.inputs)}")
        if h.screenshot_s is not None:
            print(f"    a launcher screenshot was taken {h.screenshot_s:+.2f} s from it")


def _print_coverage(result: Analysis) -> None:
    cov = result.coverage
    if not cov:
        return
    print(f"\nInput coverage: {cov['fired']} of {cov['allowed']} allowed actions fired "
          f"({cov['input_system']} input, seed {cov['seed']})")
    for a in cov["actions"]:
        state = "excluded" if a["excluded"] else ("never fired" if a["fired"] == 0 else f"{a['fired']} times")
        print(f"  {a['name']:<28} {a['value_type']:<7} {a['keys']:<24} {state}")
    for c in result.agent_commands:
        print(f"  agent at {c['t_ms'] / 1000:.2f} s: {c['command']}")


KIND_LABEL = {"cpp": "C++", "stat": "C++ stat", "gpu": "GPU pass", "object": "object", "blueprint": "Blueprint",
              "uobject": "UObject", "wait": "wait", "unknown": "?"}


def _scope_text(scope) -> str:
    where = scope.source or "no source found"
    more = f" (+{scope.more_sites} more sites)" if scope.more_sites else ""
    return f"[{KIND_LABEL.get(scope.kind, scope.kind)}] {where}{more}"


def _trigger_text(tr) -> str:
    where = f" ({tr.scope.source})" if tr.scope.source else ""
    return f"{tr.how} {tr.name}{where}"


def _print_explanation(expl, indent: str = "    ") -> None:
    if not expl.tree and not expl.hotspots:
        print(f"{indent}no scope rose above its baseline")
    for h in expl.hotspots[:8]:
        print(f"{indent}+{h.rise_ms:6.2f} ms  {h.scope.source or 'no source location'}")
        print(f"{indent}           scope {h.path[-1]} [{KIND_LABEL.get(h.scope.kind, h.scope.kind)}] on {h.thread}")
        for tr in h.triggered_by:
            print(f"{indent}           {_trigger_text(tr)}")
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
    print(f"\nTrace: {report.frames_read} hitch and overrun frames read, {report.baseline_frames} baseline frames, "
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

    run_info = read_run_info(run / "run_info.txt")
    result = analyze(frames, thresholds, markers, phase_map, run_info,
                     load_inputs(run / "inputs.csv"), load_coverage(run / "actions.csv"), screenshot_times(run, run_info))
    output = result.to_dict()
    _print_analysis(result)
    _print_coverage(result)
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


RUNS_ROOT = Path(__file__).resolve().parent.parent / "runs"


def cmd_run(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    count = args.runs if args.runs is not None else profile.runs
    failed = 0
    for n in range(1, count + 1):
        print(f"\n=== Run {n} of {count} ===")
        try:
            result = run_playtest(profile, Path(args.runs_root), game_map=args.map, play_s=args.play_s, exe=args.build,
                                  trace=not args.no_trace)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 1
        if not (result.run / "frames.csv").exists():
            print(f"No frames.csv in {result.run}; the plugin did not record this run.", file=sys.stderr)
            failed += 1
            continue
        if not args.no_analyze:
            code = cmd_analyze(argparse.Namespace(
                run=str(result.run), profile=args.profile, budget_ms=args.budget_ms, thresholds=args.thresholds,
                no_trace=args.no_trace, insights=args.insights, project=None, reindex=False))
            failed += code != 0
    return 1 if failed else 0


def _run_arg(run: str | None) -> Path | None:
    path = Path(run) if run else latest_run(RUNS_ROOT)
    if path is None or not path.exists():
        print("No run folder given and no latest run found.", file=sys.stderr)
        return None
    return path.resolve()


def cmd_send(args: argparse.Namespace) -> int:
    run = _run_arg(args.run)
    if run is None:
        return 1
    send_command(run, " ".join(args.command))
    print(f"Sent '{' '.join(args.command)}' to {run.name}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run = _run_arg(args.run)
    if run is None:
        return 1
    meta = json.loads((run / "run.json").read_text(encoding="utf-8")) if (run / "run.json").exists() else {}
    print(f"{run}  {meta.get('state', '?')}{'  ended: ' + meta['ended'] if meta.get('ended') else ''}")
    print(json.dumps(read_status(run), indent=2))
    shots = sorted((run / "screens").glob("*.png"))
    if shots:
        print(f"latest screenshot: {shots[-1]}")
    for line in meta.get("attention", []):
        print(line)
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    project = Path(args.project)
    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "profiles" / f"{project.stem}.toml"
    if out.exists() and not args.force:
        print(f"{out} exists; pass --force to replace it, or --out for another file", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft_profile(project), encoding="utf-8")
    print(f"Wrote {out}; settle its TODOs, then run a short trial with ueperf run")
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

    p = sub.add_parser("run", help="play the game with the profile's input rules, capture and analyze")
    p.add_argument("profile", type=Path, help="game profile TOML")
    p.add_argument("--runs", type=int, default=None, help="number of runs (default: the profile's runs)")
    p.add_argument("--play-s", type=float, default=None, help="seconds of play per run (default: the profile's play_s)")
    p.add_argument("--map", default=None, help="map to start in (default: the profile's first map)")
    p.add_argument("--build", type=Path, default=None, help="packaged game executable (default: the profile's build)")
    p.add_argument("--runs-root", type=Path, default=RUNS_ROOT, help="folder for run folders")
    p.add_argument("--no-trace", action="store_true", help="record frames only, no Insights trace")
    p.add_argument("--no-analyze", action="store_true", help="skip analysis after each run")
    p.add_argument("--budget-ms", type=float, default=None, help="frame budget in ms for analysis")
    p.add_argument("--thresholds", type=Path, default=None, help="thresholds TOML file")
    p.add_argument("--insights", type=Path, default=None, help="path to UnrealInsights.exe")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("send", help="send a command to the input driver of a running playtest")
    p.add_argument("command", nargs="+", help="pause, resume, key <Key> [s], click <x> <y>, type <text>, "
                                              "action <Name> [s], exec <console command> or quit")
    p.add_argument("--run", default=None, help="run folder (default: the latest run)")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("status", help="show a playtest's live status and latest screenshot")
    p.add_argument("run", nargs="?", default=None, help="run folder (default: the latest run)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("onboard", help="draft a game profile from a project's files")
    p.add_argument("project", type=Path, help="the project's .uproject")
    p.add_argument("--out", default=None, help="profile to write (default: profiles/<project>.toml)")
    p.add_argument("--force", action="store_true", help="replace an existing profile")
    p.set_defaults(func=cmd_onboard)

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
