"""Renders a run's analysis as one self-contained HTML page with inline SVG charts."""

from __future__ import annotations

import html
import json
from pathlib import Path

from ueperf.ingest.frames import Frame, load_frames
from ueperf.phases import NON_GAMEPLAY
from ueperf.report.causes_html import STYLE as CAUSE_STYLE
from ueperf.report.causes_html import gains_html, hitches_html, overruns_html

CHART_W = 1100
CHART_H = 260
PAD_L, PAD_R, PAD_T, PAD_B = 52, 16, 14, 30

THREADS = [("game", "Game"), ("render", "Render"), ("rhi", "RHI"), ("gpu", "GPU")]

STYLE = """
.viz-root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --text-primary: #0b0b0b; --text-secondary: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --series-4: #eda100;
  --critical: #d03b3b; --serious: #ec835a; --band: rgba(137,135,129,0.12); --overrun: rgba(236,131,90,0.16);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
    --band: rgba(137,135,129,0.16); --overrun: rgba(236,131,90,0.20);
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --text-primary: #f0efec; --text-secondary: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --series-4: #c98500;
  --band: rgba(137,135,129,0.16); --overrun: rgba(236,131,90,0.20);
}
body { margin: 0; background: var(--page); }
.viz-root { font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--text-primary);
  background: var(--page); padding: 24px 16px 48px; }
main { max-width: 1140px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 8px; }
.sub { color: var(--text-secondary); margin: 0 0 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.tile .v { font-size: 20px; font-weight: 600; }
.tile .k { color: var(--text-secondary); font-size: 12px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 12px; position: relative; }
.card svg { width: 100%; height: auto; display: block; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--text-secondary); font-size: 12px; margin: 0 0 6px; }
.legend i { display: inline-block; width: 14px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.legend i.dot { width: 8px; height: 8px; border-radius: 50%; }
.legend i.box { width: 12px; height: 10px; border-radius: 2px; }
.tip { position: absolute; pointer-events: none; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.12); display: none;
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td code, li code { font-size: 12px; overflow-wrap: anywhere; }
td.timer { max-width: 520px; }
ul.causes { margin: 0; padding-left: 16px; }
.muted { color: var(--muted); }
.note { color: var(--text-secondary); font-size: 13px; margin: 6px 0 0; }
pre { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 10px; overflow-x: auto; font-size: 12px; }
"""

SCRIPT = """
for (const card of document.querySelectorAll('.card[data-chart]')) {
  const data = card._data;
  const svg = card.querySelector('svg'), tip = card.querySelector('.tip'), cross = svg.querySelector('.cross');
  const vb = svg.viewBox.baseVal, { x0, x1, t0, t1 } = data.scale;
  svg.addEventListener('pointermove', (ev) => {
    const r = svg.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width * vb.width;
    if (x < x0 || x > x1) { tip.style.display = 'none'; cross.style.display = 'none'; return; }
    const t = t0 + (x - x0) / (x1 - x0) * (t1 - t0);
    let lo = 0, hi = data.t.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (data.t[m] < t) lo = m + 1; else hi = m; }
    const i = lo, px = x0 + (data.t[i] - t0) / (t1 - t0) * (x1 - x0);
    cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.style.display = '';
    tip.innerHTML = data.fmt(i);
    tip.style.display = 'block';
    const cx = (px / vb.width) * r.width, left = cx + 14 + tip.offsetWidth > r.width ? cx - tip.offsetWidth - 14 : cx + 14;
    tip.style.left = (left + 12) + 'px'; tip.style.top = '40px';
  });
  svg.addEventListener('pointerleave', () => { tip.style.display = 'none'; cross.style.display = 'none'; });
}
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    span = max(hi - lo, 1e-9)
    raw = span / count
    mag = 10 ** int(f"{raw:e}".split("e")[1])
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = (lo // step) * step
    ticks, v = [], first
    while v <= hi + 1e-9:
        if v >= lo - 1e-9:
            ticks.append(round(v, 6))
        v += step
    return ticks


class _Scale:
    def __init__(self, t0: float, t1: float, y_max: float):
        self.t0, self.t1, self.y_max = t0, t1, y_max
        self.x0, self.x1 = PAD_L, CHART_W - PAD_R
        self.y0, self.y1 = CHART_H - PAD_B, PAD_T

    def x(self, t: float) -> float:
        return self.x0 + (t - self.t0) / max(self.t1 - self.t0, 1e-9) * (self.x1 - self.x0)

    def y(self, v: float) -> float:
        return self.y0 - min(v, self.y_max) / self.y_max * (self.y0 - self.y1)


def _axes(s: _Scale, unit: str = "ms") -> str:
    parts = []
    for v in _nice_ticks(0, s.y_max):
        y = s.y(v)
        parts.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{s.x0 - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="var(--muted)">{v:g}</text>')
    for v in _nice_ticks(s.t0, s.t1, 10):
        x = s.x(v)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{s.y0}" y2="{s.y0 + 4}" stroke="var(--axis)"/>')
        parts.append(f'<text x="{x:.1f}" y="{s.y0 + 17}" text-anchor="middle" font-size="11" fill="var(--muted)">{v:g} s</text>')
    parts.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{s.y0}" y2="{s.y0}" stroke="var(--axis)" stroke-width="1"/>')
    parts.append(f'<text x="12" y="{s.y1 + 4}" font-size="11" fill="var(--muted)">{_e(unit)}</text>')
    return "".join(parts)


def _polyline(frames: list[Frame], s: _Scale, value, color: str) -> str:
    """Line through every frame, decimated to the max per pixel column so spikes survive."""
    columns: dict[int, tuple[float, float]] = {}
    for f in frames:
        x = s.x(f.t_ms / 1000.0)
        col = int(x * 2)  # half-pixel columns
        v = value(f)
        if col not in columns or v > columns[col][1]:
            columns[col] = (x, v)
    pts = " ".join(f"{x:.1f},{s.y(v):.1f}" for _, (x, v) in sorted(columns.items()))
    return (f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')


def _data_blob(frames: list[Frame], scale: _Scale, offset_s: float, fields: list[str]) -> str:
    data = {
        "scale": {"x0": scale.x0, "x1": scale.x1, "t0": scale.t0, "t1": scale.t1},
        "t": [round(f.t_ms / 1000.0, 4) for f in frames],
        "frame": [f.frame for f in frames],
        "offset": offset_s,
    }
    for name in fields:
        data[name] = [round(getattr(f, name), 2) for f in frames]
    # JSON can't carry the formatter, so the page script attaches it after parsing.
    return json.dumps(data, separators=(",", ":"))


def _frame_chart(frames, analysis, causes, budget) -> str:
    t0, t1 = frames[0].t_ms / 1000.0, frames[-1].t_ms / 1000.0
    p99 = analysis["summary"]["p99_ms"]
    y_max = max(budget * 2.5, p99 * 1.5)
    s = _Scale(t0, t1, y_max)
    parts = [f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="Frame time over the run against the budget">']

    phases = analysis.get("phases") or []
    if phases:
        # Bands per phase: startup and loading shaded, every phase named along the top.
        for k, p in enumerate(phases):
            xa = s.x(p["start_ms"] / 1000.0)
            xb = s.x(phases[k + 1]["start_ms"] / 1000.0) if k + 1 < len(phases) else s.x1
            if p["phase"] in NON_GAMEPLAY:
                parts.append(f'<rect x="{xa:.1f}" y="{s.y1}" width="{max(xb - xa, 1):.1f}" height="{s.y0 - s.y1}" fill="var(--band)"/>')
            if xb - xa >= 60:  # narrow bands keep their hover title only
                parts.append(f'<text x="{xa + 4:.1f}" y="{s.y1 + 12}" font-size="11" fill="var(--text-secondary)">{_e(p["phase"])}</text>')
            parts.append(f'<rect x="{xa:.1f}" y="{s.y1}" width="{max(xb - xa, 1):.1f}" height="8" fill="transparent">'
                         f'<title>{_e(p["phase"])} from {p["start_ms"] / 1000:.2f} s</title></rect>')
        gameplay_from = next((p["start_ms"] / 1000.0 for p in phases if p["phase"] not in NON_GAMEPLAY), t1)
        startup_end = gameplay_from
    else:
        startup_end = t0 + (causes.get("skip_start_s", 2.0) if causes else 0.0)
        if causes is not None:
            start_x = s.x(startup_end)
            worst = max((f.frame_ms for f in frames if f.t_ms / 1000.0 < startup_end), default=0.0)
            parts.append(f'<rect x="{s.x0}" y="{s.y1}" width="{start_x - s.x0:.1f}" height="{s.y0 - s.y1}" fill="var(--band)"/>')
            parts.append(f'<text x="{start_x + 4:.1f}" y="{s.y1 + 12}" font-size="11" fill="var(--text-secondary)">'
                         f'startup, worst {worst:.0f} ms</text>')
    for o in analysis["overruns"]:
        xa, xb = s.x(o["start_ms"] / 1000.0), s.x(o["end_ms"] / 1000.0)
        parts.append(f'<rect x="{xa:.1f}" y="{s.y1}" width="{max(xb - xa, 2):.1f}" height="{s.y0 - s.y1}" fill="var(--overrun)"/>')

    parts.append(_axes(s))
    yb = s.y(budget)
    parts.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{yb:.1f}" y2="{yb:.1f}" stroke="var(--text-secondary)" '
                 f'stroke-width="1" stroke-dasharray="4 4"/>')
    parts.append(f'<text x="{s.x1 - 4}" y="{yb - 5:.1f}" text-anchor="end" font-size="11" '
                 f'fill="var(--text-secondary)">budget {budget:g} ms</text>')
    # Lifecycle markers: a hairline each, labels stacked in three rows so neighbours don't collide.
    last_x = [-1e9, -1e9, -1e9]
    for m in analysis.get("markers") or []:
        if m["name"] in ("run_start", "run_end"):
            continue
        x = s.x(m["t_ms"] / 1000.0)
        parts.append(f'<g><title>{_e(m["name"])} {_e(m["detail"])} at {m["t_ms"] / 1000:.2f} s</title>'
                     f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{s.y1}" y2="{s.y0}" stroke="var(--muted)" stroke-width="1" '
                     f'stroke-dasharray="2 3"/></g>')
        row = next((r for r in range(3) if x - last_x[r] > 90), None)
        if row is not None:  # no free row: the hover title still names it
            last_x[row] = x
            parts.append(f'<text x="{x + 3:.1f}" y="{s.y0 - 6 - row * 13}" font-size="10" fill="var(--muted)">{_e(m["name"])}</text>')
    ld = analysis.get("loading") or {}
    if ld.get("playable_run_s") is not None:
        x = s.x(ld["playable_run_s"])
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{s.y1}" y2="{s.y0}" stroke="var(--series-3)" stroke-width="1.5"/>')
        parts.append(f'<text x="{x + 4:.1f}" y="{s.y1 + 26}" font-size="11" font-weight="600" fill="var(--text-primary)">'
                     f'playable</text>')
    parts.append(_polyline(frames, s, lambda f: f.frame_ms, "var(--series-1)"))

    for n, h in enumerate(analysis["hitches"], 1):
        x, v = s.x(h["start_ms"] / 1000.0), h["peak_ms"]
        y = s.y(v)
        clipped = v > y_max
        parts.append(f'<g><title>H{n}: {v:.1f} ms at {h["start_ms"] / 1000:.2f} s</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="var(--critical)" stroke="var(--surface-1)" stroke-width="2"/></g>')
        label = f"H{n}" + (f" {v:.0f} ms" if clipped else "")
        parts.append(f'<text x="{x + 8:.1f}" y="{y + 4:.1f}" font-size="11" font-weight="600" fill="var(--text-primary)">{label}</text>')
    for f in frames:
        if f.frame_ms > y_max and f.t_ms / 1000.0 >= startup_end and not any(h["start_frame"] <= f.frame <= h["end_frame"] for h in analysis["hitches"]):
            x = s.x(f.t_ms / 1000.0)
            parts.append(f'<text x="{x + 4:.1f}" y="{s.y1 + 26}" font-size="10" fill="var(--muted)">{f.frame_ms:.0f} ms</text>')

    parts.append(f'<line class="cross" x1="0" x2="0" y1="{s.y1}" y2="{s.y0}" stroke="var(--muted)" stroke-width="1" style="display:none"/>')
    parts.append("</svg>")
    offset = causes["trace_offset_s"] if causes else 0.0
    blob = _data_blob(frames, s, offset, ["frame_ms", "game_ms", "render_ms", "rhi_ms", "gpu_ms"])
    legend = ('<div class="legend"><span><i style="background:var(--series-1)"></i>Frame time</span>'
              '<span><i style="background:none;border-top:1px dashed var(--text-secondary)"></i>Budget</span>'
              '<span><i class="dot" style="background:var(--critical)"></i>Hitch</span>'
              '<span><i class="box" style="background:var(--overrun)"></i>Sustained overrun</span>'
              '<span><i class="box" style="background:var(--band)"></i>Startup, loading or warmup</span>'
              '<span><i style="background:var(--series-3)"></i>Playable</span>'
              '<span><i style="background:none;border-top:1px dotted var(--muted)"></i>Lifecycle marker</span></div>')
    note = f'<p class="note">Frames above {y_max:.0f} ms are drawn at the top edge; their value is printed beside them.</p>'
    return (f'<div class="card" data-chart="frames">{legend}{"".join(parts)}<div class="tip"></div>'
            f'<script type="application/json">{blob}</script></div>{note}')


def _thread_chart(frames, analysis, budget) -> str:
    t0, t1 = frames[0].t_ms / 1000.0, frames[-1].t_ms / 1000.0
    vals = sorted(max(f.game_ms, f.render_ms, f.rhi_ms, f.gpu_ms) for f in frames)
    p99 = vals[int(len(vals) * 0.99) - 1] if vals else budget
    y_max = max(budget * 1.5, p99 * 1.3)
    s = _Scale(t0, t1, y_max)
    parts = [f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="Game, render, RHI and GPU time per frame">', _axes(s)]
    colors = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"]
    for (name, _), color in zip(THREADS, colors):
        parts.append(_polyline(frames, s, lambda f, n=name: getattr(f, f"{n}_ms"), color))
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{s.y1}" y2="{s.y0}" stroke="var(--muted)" stroke-width="1" style="display:none"/>')
    parts.append("</svg>")
    legend = '<div class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{label}</span>' for (_, label), c in zip(THREADS, colors)) + "</div>"
    blob = _data_blob(frames, s, 0.0, ["frame_ms", "game_ms", "render_ms", "rhi_ms", "gpu_ms"])
    return (f'<div class="card" data-chart="threads">{legend}{"".join(parts)}<div class="tip"></div>'
            f'<script type="application/json">{blob}</script></div>')


def _phases_table(analysis) -> str:
    phases, markers = analysis.get("phases") or [], analysis.get("markers") or []
    if not phases:
        return ""
    rows = "".join(
        f'<tr><td>{_e(p["phase"])}</td><td class="n">{p["start_ms"] / 1000:.2f}</td><td class="n">{p["duration_s"]:.1f}</td>'
        f'<td class="n">{p["frames"]}</td><td class="n">{p["median_ms"]:.2f}</td><td class="n">{p["p95_ms"]:.2f}</td>'
        f'<td class="n">{p["p99_ms"]:.2f}</td><td class="n">{p["frames_over"]}</td></tr>' for p in phases)
    marker_rows = "".join(
        f'<tr><td class="n">{m["t_ms"] / 1000:.2f}</td><td class="n">{m["frame"]}</td><td><code>{_e(m["name"])}</code></td>'
        f'<td>{_e(m["detail"])}</td><td>{"trace scope" if m["source"] == "trace" else "plugin"}</td></tr>' for m in markers)
    return ('<h2>Lifecycle phases</h2><div class="scroll"><table><thead><tr><th>Phase</th><th class="n">From s</th>'
            '<th class="n">Length s</th><th class="n">Frames</th><th class="n">Median ms</th><th class="n">p95</th>'
            f'<th class="n">p99</th><th class="n">Over budget</th></tr></thead><tbody>{rows}</tbody></table></div>'
            '<details><summary>Lifecycle markers</summary><div class="scroll"><table><thead><tr><th class="n">Run s</th>'
            '<th class="n">Frame</th><th>Marker</th><th>Detail</th><th>Found by</th></tr></thead>'
            f'<tbody>{marker_rows}</tbody></table></div></details>')


def _loading_html(analysis) -> str:
    ld = analysis.get("loading")
    if not ld:
        return ""

    def s(v):
        return f"{v:.2f} s" if v is not None else "n/a"

    tiles = [
        (s(ld["launch_to_playable_s"]), "Launch to playable"),
        (s(ld["launch_to_recording_s"]), "Launch to engine up"),
        (s(ld["map_load_s"]), "Map load"),
        (s(ld["map_loaded_to_playable_s"]), "Map loaded to playable"),
    ]
    note = (f'Playable is the first frame that starts a {ld["window_s"]:g} s stretch with at most '
            f'{ld["max_share_over"]:.0%} of frames over budget and none over {ld.get("max_frame_ratio", 1.5):g}× the '
            'budget. Frames from the map load until then are the '
            '<b>warmup</b> phase: shown in the chart and hitch list, but left out of baselines and the gains ranking.')
    if ld["playable_frame"] is None:
        note = 'The frame rate never became steady in this run, so there is no playable time. ' + note
    return ('<h2>Loading</h2><div class="tiles">' + "".join(
        f'<div class="tile"><div class="v">{_e(v)}</div><div class="k">{_e(k)}</div></div>' for v, k in tiles)
        + f'</div><p class="note">{note}</p>')


def _tiles(summary, n_hitches, n_overruns) -> str:
    tiles = [
        (f'{summary["median_ms"]:.2f} ms', "Median frame"),
        (f'{summary["p95_ms"]:.2f} ms', "p95"),
        (f'{summary["p99_ms"]:.2f} ms', "p99"),
        (f'{summary["max_ms"]:.1f} ms', "Worst frame"),
        (f'{summary["share_over"]:.1%}', f'Over budget ({summary["frames_over"]} frames)'),
        (str(n_hitches), "Hitches"),
        (str(n_overruns), "Sustained overruns"),
    ]
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="v">{_e(v)}</div><div class="k">{_e(k)}</div></div>' for v, k in tiles) + "</div>"


FORMATTER = """
<script>
for (const card of document.querySelectorAll('.card[data-chart]')) {
  const node = card.querySelector('script[type="application/json"]');
  const d = JSON.parse(node.textContent);
  d.fmt = (i) => `<b>${d.t[i].toFixed(2)} s</b> &middot; frame ${d.frame[i]}<br>` +
    `frame ${d.frame_ms[i].toFixed(2)} ms<br>game ${d.game_ms[i].toFixed(2)} &middot; render ${d.render_ms[i].toFixed(2)}` +
    ` &middot; RHI ${d.rhi_ms[i].toFixed(2)} &middot; GPU ${d.gpu_ms[i].toFixed(2)}` +
    (d.offset ? `<br><span style="color:var(--muted)">trace &asymp; ${(d.t[i] + d.offset).toFixed(3)} s</span>` : '');
  node.textContent = '';
  card._data = d;
}
</script>
"""


def render_report(run: Path, analysis: dict, frames: list[Frame], run_info: str = "") -> str:
    summary = analysis["summary"]
    causes = analysis.get("causes")
    budget = summary["budget_ms"]
    name = run.name

    trace_note = ""
    if causes:
        trace_note = (f'<p class="note">Each hitch lists the scopes whose <b>own</b> time rose above their usual cost '
                      f'on {causes["baseline_frames"]} in-budget frames: the time spent in that scope itself, not in '
                      'instrumented scopes below it. Each row leads with the source to open; the callers dropdown shows '
                      'how it was reached, with framework scopes such as the engine loop left out. The full call tree '
                      'appears only when no few scopes explain the stall. Trace times are on the Insights clock, about '
                      f'{causes["trace_offset_s"]:.3f} s ahead of run time.</p>')

    sections = [
        f'<h1>{_e(name)}</h1>',
        f'<p class="sub">{summary["frames"]} frames over {summary["duration_s"]:.1f} s, budget {budget:g} ms</p>',
        _tiles(summary, len(analysis["hitches"]), len(analysis["overruns"])),
        _loading_html(analysis),
        '<h2>Frame time</h2>', _frame_chart(frames, analysis, causes, budget),
        '<h2>Thread and GPU time per frame</h2>', _thread_chart(frames, analysis, budget),
        _phases_table(analysis),
        '<h2>Hitches</h2>', trace_note, hitches_html(analysis, causes),
        '<h2>Sustained overruns</h2>', overruns_html(analysis, causes),
    ]
    if causes:
        sections += ['<h2>Gains ranking</h2>',
                     f'<p class="note">Hot spots across {causes["frames_read"]} of {causes["frames_over"]} gameplay '
                     'over-budget frames: each one\'s own rise over its usual cost, scaled so a frame never gives out more '
                     f'than its excess, summed over the run. {causes["non_gameplay_frames_over"]} over-budget frames '
                     'during startup or loading are left out.</p>', gains_html(causes)]
    else:
        sections += ['<h2>Gains ranking</h2>', '<p class="note">No trace was analyzed for this run.</p>']
    if run_info:
        sections += ['<h2>Run info</h2>', f'<pre>{_e(run_info)}</pre>']

    body = "".join(sections)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_e(name)} perf report</title><style>{STYLE}{CAUSE_STYLE}</style></head>'
            f'<body><div class="viz-root"><main>{body}</main></div>{FORMATTER}<script>{SCRIPT}</script></body></html>')


def write_report(run: Path) -> Path:
    """Render `run`/report.html from its analysis.json, frames.csv and run_info.txt."""
    analysis = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    frames = load_frames(run / "frames.csv")
    info_path = run / "run_info.txt"
    run_info = info_path.read_text(encoding="utf-8") if info_path.exists() else ""
    out = run / "report.html"
    out.write_text(render_report(run, analysis, frames, run_info), encoding="utf-8")
    return out
