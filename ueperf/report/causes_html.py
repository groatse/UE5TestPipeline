"""Report sections that explain hitches, overruns and gains as call paths."""

from __future__ import annotations

import html

KIND_LABEL = {"cpp": "C++", "stat": "C++ stat", "gpu": "GPU pass", "object": "Object", "blueprint": "Blueprint",
              "uobject": "UObject", "wait": "Wait", "unknown": "Unknown"}

STYLE = """
.expl { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin: 0 0 12px; }
.expl h3 { font-size: 14px; margin: 0 0 4px; }
.expl .meta { color: var(--text-secondary); font-size: 12px; margin: 0 0 10px; font-variant-numeric: tabular-nums; }
.kind { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--border);
  color: var(--text-secondary); white-space: nowrap; }
.src { color: var(--text-secondary); font-size: 12px; overflow-wrap: anywhere; }
.path { font: 12px/1.5 ui-monospace, Consolas, monospace; color: var(--text-secondary); overflow-wrap: anywhere; }
.path b { color: var(--text-primary); font-weight: 600; }
.path .bp { color: var(--series-2); }
.tree td { padding: 3px 10px; border-bottom: none; }
.tree td.name { font: 12px/1.5 ui-monospace, Consolas, monospace; white-space: nowrap; }
.tree tr.wait td.name { color: var(--muted); }
details > summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; margin: 8px 0 4px; }
.waits { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
"""


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _kind(scope: dict) -> str:
    return f'<span class="kind">{_e(KIND_LABEL.get(scope["kind"], scope["kind"]))}</span>'


def _source(scope: dict) -> str:
    if not scope["source"]:
        return '<span class="src">no source location in trace or source</span>'
    more = f' <span class="muted">(+{scope["more_sites"]} more sites)</span>' if scope.get("more_sites") else ""
    return f'<span class="src">{_e(scope["source"])}</span>{more}'


def path_html(path: list[str]) -> str:
    """Breadcrumb of a call path; Blueprint and object scopes stand out, the leaf is bold."""
    parts = []
    for i, name in enumerate(path):
        cls = ' class="bp"' if (name.startswith("ExecuteUbergraph_") or name.endswith("_C") or " /" in name) else ""
        text = f"<b>{_e(name)}</b>" if i == len(path) - 1 else f"<span{cls}>{_e(name)}</span>"
        parts.append(text)
    return '<div class="path">' + " › ".join(parts) + "</div>"


def _hotspot_table(hotspots: list[dict]) -> str:
    if not hotspots:
        return '<p class="note">No scope\'s own time rose above its baseline.</p>'
    rows = "".join(
        f'<tr><td class="n">+{h["rise_ms"]:.2f}</td><td class="n">{h["self_ms"]:.2f}</td>'
        f'<td class="n">{h["baseline_ms"]:.2f}</td><td>{_e(h["thread"])}</td>'
        f'<td><code>{_e(h["path"][-1])}</code> {_kind(h["scope"])}<br>{_source(h["scope"])}{path_html(h["path"])}</td></tr>'
        for h in hotspots)
    return ('<div class="scroll"><table><thead><tr><th class="n">Rise ms</th><th class="n">Own ms</th>'
            '<th class="n">Usual ms</th><th>Thread</th><th>Scope, source and call path</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def _tree_table(tree: list[dict]) -> str:
    if not tree:
        return ""
    rows = []
    for n in tree:
        pad = n["depth"] * 16
        own = f'+{n["self_rise_ms"]:.2f}' if n["self_rise_ms"] >= 0.05 else ""
        wait = ' class="wait"' if n["scope"]["kind"] == "wait" else ""
        rows.append(
            f'<tr{wait}><td>{_e(n["thread"])}</td><td class="name" style="padding-left:{10 + pad}px">{_e(n["path"][-1])}</td>'
            f'<td class="n">{n["ms"]:.2f}</td><td class="n">+{n["rise_ms"]:.2f}</td><td class="n">{own}</td>'
            f'<td>{_kind(n["scope"])} {_source(n["scope"])}</td></tr>')
    return ('<details><summary>Call tree of scopes whose time rose</summary><div class="scroll"><table class="tree"><thead><tr>'
            '<th>Thread</th><th>Scope</th><th class="n">Incl ms</th><th class="n">Rise</th><th class="n">Own rise</th>'
            f'<th>Source</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></details>')


def explanation_html(title: str, meta: str, expl: dict | None) -> str:
    if expl is None:
        body = '<p class="note">No trace was analyzed.</p>'
    else:
        waits = ""
        if expl["waits"]:
            waits = '<p class="waits">Bound thread waiting: ' + "; ".join(
                f'{_e(w["thread"])} <code>{_e(w["timer"])}</code> +{w["rise_ms"]:.2f} ms' for w in expl["waits"]) + \
                ". Worker scopes that rose are counted as the work it waited for.</p>"
        body = _hotspot_table(expl["hotspots"]) + waits + _tree_table(expl["tree"])
    return f'<div class="expl"><h3>{_e(title)}</h3><p class="meta">{meta}</p>{body}</div>'


def hitches_html(analysis: dict, causes: dict | None) -> str:
    if not analysis["hitches"]:
        return '<p class="note">No hitches.</p>'
    out = []
    for n, h in enumerate(analysis["hitches"], 1):
        window = causes["hitch_windows"][n - 1] if causes else None
        at = f' · trace {window[0]:.3f} to {window[1]:.3f} s' if window else ""
        meta = (f'run {h["start_ms"] / 1000:.2f} s{at} · frame {h["start_frame"]}'
                + (f'-{h["end_frame"]}' if h["end_frame"] != h["start_frame"] else "")
                + f' · {h["peak_ms"]:.1f} ms vs usual {h["baseline_ms"]:.1f} ms · stall {h["stall_ms"]:.1f} ms · {_e(h["bound"])}-bound')
        out.append(explanation_html(f"H{n}", meta, causes["hitches"][n - 1] if causes else None))
    return "".join(out)


def overruns_html(analysis: dict, causes: dict | None) -> str:
    if not analysis["overruns"]:
        return '<p class="note">No sustained overruns.</p>'
    out = []
    for n, o in enumerate(analysis["overruns"], 1):
        meta = (f'run {o["start_ms"] / 1000:.2f} to {o["end_ms"] / 1000:.2f} s · {o["frames_over"]} of {o["frames"]} frames '
                f'over · excess {o["total_excess_ms"]:.1f} ms · peak {o["peak_ms"]:.1f} ms · {_e(o["bound"])}-bound · '
                'times are per frame, averaged')
        out.append(explanation_html(f"O{n}", meta, causes["overruns"][n - 1] if causes else None))
    return "".join(out)


def gains_html(causes: dict) -> str:
    if not causes["gains"]:
        return '<p class="note">No over-budget gameplay frames, so there is nothing to rank.</p>'
    rows = "".join(
        f'<tr><td class="n">{n}</td><td class="n">{g["credit_ms"]:.2f}</td><td class="n">{g["frames"]}</td>'
        f'<td><code>{_e(g["path"][-1])}</code> {_kind(g["scope"])} <span class="muted">{_e(g["thread"])}</span><br>'
        f'{_source(g["scope"])}{path_html(g["path"])}</td>'
        f'<td class="n">{", ".join("%.3f" % r["start_s"] for r in g["worst"])}</td></tr>'
        for n, g in enumerate(causes["gains"], 1))
    return ('<div class="scroll"><table><thead><tr><th class="n">#</th><th class="n">Over-budget ms</th>'
            '<th class="n">Frames</th><th>Hot spot, source and call path</th><th class="n">Worst at, trace s</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>')


def hitch_groups_html(causes: dict | None) -> str:
    if not causes or not causes["hitch_groups"]:
        return ""
    rows = "".join(
        f'<tr><td class="n">{g["hitches"]}</td><td class="n">{g["stall_ms"]:.1f}</td>'
        f'<td>{_e(g["thread"])}</td><td>{path_html(g["path"])}</td></tr>' for g in causes["hitch_groups"])
    return ('<h2>Hitches by top hot spot</h2><div class="scroll"><table><thead><tr><th class="n">Hitches</th>'
            f'<th class="n">Stall ms</th><th>Thread</th><th>Call path</th></tr></thead><tbody>{rows}</tbody></table></div>')
