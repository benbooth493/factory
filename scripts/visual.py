#!/usr/bin/env python3
"""visual.py — turn a JSON spec into one self-contained HTML page.

Commands (run from the repo root):

  visual.py facts [--base REF]             recap spec skeleton for this branch (JSON)
  visual.py scan <dir> [--max-nodes 60]    codebase-map spec for a directory (JSON)
  visual.py render <spec.json> [--out FILE] [--source-base URL]
                                           write the page; prints {"out", "bytes"}

The spec is gathered by Claude (or by facts/scan) and the page is rendered
from it deterministically: same spec, same bytes. Everything is inline (CSS,
SVG diagrams, a little JS) and nothing is fetched, so the page works offline
and can be published as-is. Untrusted text is always escaped.

Spec shape (every field optional except kind and title):

  kind        recap | plan | map
  title, subtitle, summary (light markdown), meta {label: value}
  highlights  [{title, detail}]
  diagrams    [{title, caption, columns: [titles], align: center|top, nodes, edges}]
              node {id, label, sub, kind, change, detail, source, col, row, w}
              edge {from, to, label, style}
              change: added | modified | removed | planned
  steps       [{title, detail, files}]       (plan; drawn as a flow when no diagram)
  commits     [{sha, subject, author}]
  files       [{path, status, added, removed, note}]
  risks       [{level: high|med|low, text}]
  tests       [{name, status: pass|fail|skip|unknown, cmd, detail}]
  questions   [{q, default}]
  sections    [{title, body}]

source is "path", "path:12" or "path:12-20" (or a list of them). With
--source-base https://github.com/OWNER/REPO/blob/main/ each becomes a link,
":12-20" mapping to "#L12-L20". Without it sources stay plain text.

Default output: <repo>/.claude/factory/visual/<slug>.html. Python 3.9+, stdlib only.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

# ---------------------------------------------------------------- text helpers


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")
    return s[:60] or "page"


def css_token(s) -> str:
    return re.sub(r"[^a-z0-9-]", "", str(s or "").lower())[:24]


def safe_json(data) -> str:
    """JSON that can sit inside <script> without ending it or opening a comment."""
    out = json.dumps(data, ensure_ascii=False, sort_keys=True)
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                 (" ", "\\u2028"), (" ", "\\u2029")):
        out = out.replace(a, b)
    return out


_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|[\w./#-]+)\)"), r'<a href="\2">\1</a>'),
]


def inline_md(text: str) -> str:
    """Escape first, then add code, bold and links (http(s) or relative only)."""
    s = esc(text)
    parts = re.split(r"(<code>.*?</code>)", _INLINE[0][0].sub(_INLINE[0][1], s))
    for i, p in enumerate(parts):
        if not p.startswith("<code>"):
            for rx, rep in _INLINE[1:]:
                p = rx.sub(rep, p)
            parts[i] = p
    return "".join(parts)


def md(text: str) -> str:
    """Small markdown subset: paragraphs, - lists, 1. lists, ``` fences, | tables, ### headings."""
    if not text:
        return ""
    out, lines, i = [], str(text).replace("\r\n", "\n").split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("```"):
            j, body = i + 1, []
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                body.append(lines[j])
                j += 1
            out.append("<pre><code>%s</code></pre>" % esc("\n".join(body)))
            i = j + 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:
            level = min(6, len(m.group(1)) + 1)
            out.append("<h%d>%s</h%d>" % (level, inline_md(m.group(2)), level))
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append('<div class="tablewrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
                "".join("<th>%s</th>" % inline_md(c) for c in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline_md(c) for c in r) for r in body)))
            continue
        lm = re.match(r"\s*(?:[-*]|\d+\.)\s+", line)
        if lm:
            tag = "ol" if re.match(r"\s*\d+\.", line) else "ul"
            items = []
            while i < len(lines) and lines[i].strip():
                if re.match(r"\s*(?:[-*]|\d+\.)\s+", lines[i]):
                    items.append(re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline_md(t) for t in items), tag))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"\s*(```|\||#{1,6}\s|[-*]\s|\d+\.\s)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % inline_md(" ".join(para)))
    return "\n".join(out)


# ---------------------------------------------------------------- source links


def check_base(base: str | None) -> str | None:
    if not base:
        return None
    if not re.match(r"https?://[^\s\"'<>]+$", base):
        raise SystemExit("visual: --source-base must be an http(s) URL")
    return base.rstrip("/") + "/"


def source_link(src: str, base: str | None):
    """("path:12-20", base) -> (text, href or None). Lines map to GitHub anchors."""
    src = str(src).strip()
    if re.match(r"https?://", src):
        return src, src
    m = re.match(r"(.*?)(?::(\d+)(?:-(\d+))?)?$", src)
    path, a, b = m.group(1), m.group(2), m.group(3)
    if not base or not path or path.startswith(("/", "~")) or ".." in path.split("/"):
        return src, None
    anchor = ("#L%s" % a + ("-L%s" % b if b else "")) if a else ""
    return src, base + quote(path.lstrip("./") if path.startswith("./") else path) + anchor


def sources_of(node) -> list:
    s = node.get("source")
    if not s:
        return []
    return [x for x in (s if isinstance(s, list) else [s]) if x]


def commit_base(base: str | None) -> str | None:
    m = re.match(r"(https://github\.com/[^/]+/[^/]+)/blob/", base or "")
    return m.group(1) + "/commit/" if m else None


# ---------------------------------------------------------------- diagram layout

NODE_H, NODE_H_SUB, ROW_GAP, PAD, HEAD = 40, 54, 18, 24, 34


def node_width(n) -> int:
    if n.get("w"):
        return int(max(90, min(360, n["w"])))
    text = max(len(str(n.get("label", ""))), int(len(str(n.get("sub", ""))) * 0.85))
    return int(max(120, min(240, text * 7.6 + 32)))


def clip(s: str, width: int, px: float = 7.4) -> str:
    s = str(s or "")
    n = max(4, int((width - 26) / px))
    return s if len(s) <= n else s[: n - 1] + "…"


def layout(dia: dict) -> dict:
    """Layered left-to-right layout. Columns from `col` or longest path (cycles ignored);
    rows from `row` or a barycentre pass to keep edges short and mostly uncrossed."""
    nodes = [n for n in dia.get("nodes", []) if n.get("id") is not None]
    seen, uniq = set(), []
    for n in nodes:
        if str(n["id"]) not in seen:
            seen.add(str(n["id"]))
            uniq.append(n)
    nodes = uniq
    ids = [str(n["id"]) for n in nodes]
    byid = {str(n["id"]): n for n in nodes}
    edges = [e for e in dia.get("edges", []) if str(e.get("from")) in byid and str(e.get("to")) in byid]
    succ = {i: [] for i in ids}
    for e in edges:
        succ[str(e["from"])].append(str(e["to"]))
    # DFS in input order: drop back edges, collect a topological order
    state, order, dag = {}, [], {i: [] for i in ids}

    def visit(u):
        stack = [(u, iter(succ[u]))]
        state[u] = 1
        while stack:
            v, it = stack[-1]
            for w in it:
                if state.get(w) == 1:
                    continue  # back edge
                dag[v].append(w)
                if not state.get(w):
                    state[w] = 1
                    stack.append((w, iter(succ[w])))
                break
            else:
                state[v] = 2
                order.append(v)
                stack.pop()

    for i in ids:
        if not state.get(i):
            visit(i)
    order.reverse()
    preds = {i: [] for i in ids}
    for v in ids:
        for w in dag[v]:
            preds[w].append(v)
    col = {}
    for v in order:
        n = byid[v]
        if isinstance(n.get("col"), (int, float)):
            col[v] = int(n["col"])
        else:
            col[v] = max([col[p] + 1 for p in preds[v] if p in col] or [0])
    nbr = {i: set() for i in ids}
    for e in edges:
        a, b = str(e["from"]), str(e["to"])
        nbr[a].add(b)
        nbr[b].add(a)
    # unconnected nodes without a set position go in a grid after the graph, not one tall column
    loose = [v for v in ids if not nbr[v] and not isinstance(byid[v].get("col"), (int, float))]
    linked = [v for v in ids if v not in loose]
    if edges and loose:
        first = max(col[v] for v in linked) + 1 if linked else 0
        per = max(3, max([sum(1 for u in linked if col[u] == c) for c in set(col[u] for u in linked)] or [0]))
        for k, v in enumerate(loose):
            col[v] = first + k // per
    elif loose and len(loose) > 6:
        per = max(3, int(len(loose) ** 0.5 + 0.99))
        for k, v in enumerate(loose):
            col[v] = k // per
    cols = {}
    for v in ids:
        cols.setdefault(col[v], []).append(v)
    keys = sorted(cols)
    for _ in range(2):
        pos = {v: k for c in keys for k, v in enumerate(cols[c])}
        for c in keys:
            members = cols[c]
            if any(isinstance(byid[v].get("row"), (int, float)) for v in members):
                cols[c] = sorted(members, key=lambda v: (float(byid[v].get("row", 1e6)), members.index(v)))
                continue

            def bary(v, members=members, c=c):
                near = [pos[u] for u in nbr[v] if col[u] != c]
                return (sum(near) / len(near)) if near else members.index(v)
            cols[c] = sorted(members, key=lambda v: (bary(v), members.index(v)))
    h = NODE_H_SUB if any(n.get("sub") for n in nodes) else NODE_H
    titles = dia.get("columns") or []
    top = PAD + (HEAD if titles else 0)
    tallest = max([len(v) for v in cols.values()] or [0])
    total_h = tallest * h + max(0, tallest - 1) * ROW_GAP
    gap = 120 if any(e.get("label") for e in edges) else 80
    x, place = PAD, {}
    col_x = {}
    for c in keys:
        width = max(node_width(byid[v]) for v in cols[c])
        n_here = len(cols[c])
        offset = 0 if dia.get("align") == "top" else (total_h - (n_here * h + (n_here - 1) * ROW_GAP)) / 2
        col_x[c] = (x, width)
        for k, v in enumerate(cols[c]):
            place[v] = (x, top + offset + k * (h + ROW_GAP), node_width(byid[v]), h)
        x += width + gap
    return {"nodes": nodes, "edges": edges, "place": place, "col_x": col_x, "keys": keys,
            "width": int(x - gap + PAD) if keys else 2 * PAD, "height": int(top + total_h + PAD),
            "titles": titles}


def edge_path(a, b) -> str:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    y1, y2 = ay + ah / 2, by + bh / 2
    if bx > ax + aw / 2:  # forward
        x1, x2 = ax + aw, bx
        d = max(30, (x2 - x1) / 2)
        return "M%.1f %.1fC%.1f %.1f %.1f %.1f %.1f %.1f" % (x1, y1, x1 + d, y1, x2 - d, y2, x2 - 6, y2)
    if bx + bw < ax:  # backward: leave left, enter right
        x1, x2 = ax, bx + bw
        d = max(30, (x1 - x2) / 2)
        return "M%.1f %.1fC%.1f %.1f %.1f %.1f %.1f %.1f" % (x1, y1, x1 - d, y1, x2 + d, y2, x2 + 6, y2)
    x1, x2 = ax + aw, bx + bw  # same column: bulge out to the right
    return "M%.1f %.1fC%.1f %.1f %.1f %.1f %.1f %.1f" % (x1, y1, x1 + 46, y1, x2 + 46, y2, x2 + 6, y2)


BADGE = {"added": "NEW", "modified": "CHANGED", "removed": "REMOVED", "planned": "PLANNED"}


def render_diagram(dia: dict, idx: int, base: str | None, details: dict) -> str:
    L = layout(dia)
    place = L["place"]
    parts = ['<svg class="dia" role="img" aria-label="%s" viewBox="0 0 %d %d" width="%d" height="%d" '
             'xmlns="http://www.w3.org/2000/svg">' % (esc(dia.get("title") or "diagram"), L["width"], L["height"],
                                                       L["width"], L["height"]),
             '<defs><marker id="ah%d" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ahead"/></marker></defs>' % idx]
    titles = L["titles"]
    for k, c in enumerate(L["keys"]):
        title = titles.get(str(c)) if isinstance(titles, dict) else (titles[c] if 0 <= c < len(titles) else None)
        if title:
            x, w = L["col_x"][c]
            parts.append('<text class="colh" x="%.1f" y="%d">%s</text>' % (x, PAD + 12, esc(clip(title, w + 60, 7))))
    labels = []
    for e in L["edges"]:
        a, b = str(e["from"]), str(e["to"])
        style = css_token(e.get("style"))
        parts.append('<path class="edge%s" data-from="%s" data-to="%s" d="%s" marker-end="url(#ah%d)"/>' % (
            " s-" + style if style else "", esc(a), esc(b), edge_path(place[a], place[b]), idx))
        ax, ay, aw, ah = place[a]
        bx, by, bw, bh = place[b]
        if e.get("label") and abs(ax - bx) > 1:  # same-column labels collide with boxes; the panel has them
            mx, my = ((ax + aw + bx) / 2, (ay + by + ah) / 2) if bx > ax else ((ax + bx + bw) / 2 + 30, (ay + by + ah) / 2)
            labels.append('<text class="elabel" data-from="%s" data-to="%s" x="%.1f" y="%.1f">%s</text>' % (
                esc(a), esc(b), mx, my - 4, esc(clip(e["label"], 140, 6.4))))
    parts.extend(labels)
    for n in L["nodes"]:
        nid = str(n["id"])
        x, y, w, h = place[nid]
        kind, change = css_token(n.get("kind")), css_token(n.get("change"))
        cls = "node" + (" k-" + kind if kind else "") + (" c-" + change if change else "")
        label, sub = str(n.get("label") or nid), n.get("sub")
        g = ['<g class="%s" data-id="%s" data-dia="%d" tabindex="0" role="button" aria-label="%s" '
             'transform="translate(%.1f %.1f)">' % (cls, esc(nid), idx, esc(label), x, y),
             "<title>%s</title>" % esc(label + (" — " + str(sub) if sub else "")),
             '<rect class="box" width="%d" height="%d" rx="3"/>' % (w, h),
             '<rect class="strip" width="5" height="%d"/>' % h,
             '<text class="lbl" x="14" y="%d">%s</text>' % (24 if sub else h / 2 + 5, esc(clip(label, w, 7.6)))]
        if sub:
            g.append('<text class="sub" x="14" y="42">%s</text>' % esc(clip(sub, w, 6.4)))
        if change in BADGE:
            g.append('<text class="badge" x="%d" y="-5" text-anchor="end">%s</text>' % (w, BADGE[change]))
        g.append("</g>")
        parts.append("".join(g))
        srcs = []
        for s in sources_of(n):
            text, href = source_link(s, base)
            srcs.append({"text": text, "href": href})
        details["%d:%s" % (idx, nid)] = {
            "label": label, "sub": str(sub or ""), "kind": str(n.get("kind") or ""), "change": str(n.get("change") or ""),
            "detail": str(n.get("detail") or ""), "sources": srcs,
            "out": [[str(e["to"]), str(e.get("label") or "")] for e in L["edges"] if str(e["from"]) == nid],
            "in": [[str(e["from"]), str(e.get("label") or "")] for e in L["edges"] if str(e["to"]) == nid],
            "names": {}}
    names = {str(n["id"]): str(n.get("label") or n["id"]) for n in L["nodes"]}
    for key, d in details.items():
        if key.startswith("%d:" % idx):
            d["names"] = {i: names[i] for i, _ in d["out"] + d["in"]}
    parts.append("</svg>")
    kinds = []
    for n in L["nodes"]:
        k = str(n.get("kind") or "")
        if k and k not in kinds:
            kinds.append(k)
    legend = "".join('<span class="lg k-%s"><i></i>%s</span>' % (css_token(k), esc(k)) for k in kinds)
    return ('<figure class="diagram">%s<div class="canvas" tabindex="-1">%s</div>'
            '<figcaption>%s<span class="legend">%s</span>'
            '<button type="button" class="fit" aria-pressed="false">Fit to width</button></figcaption></figure>') % (
        "<h3>%s</h3>" % esc(dia["title"]) if dia.get("title") else "", "".join(parts),
        inline_md(dia["caption"]) if dia.get("caption") else "", legend)


# ---------------------------------------------------------------- page

CSS = """
:root{--bg:#f5f4ef;--fg:#121212;--muted:#5d5c57;--line:#121212;--card:#fff;--soft:#e8e6de;
--accent:#e8480c;--lane:#2d55c8;--script:#6f6d66;--hook:#7c3aed;--step:#0f766e;
--add:#0a7d3c;--mod:#9a6400;--del:#b3261e;--plan:#2d55c8;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#111110;--fg:#f0eee6;--muted:#a3a198;--line:#e8e6de;--card:#1b1b19;
--soft:#2a2a27;--accent:#ff7a3d;--lane:#7c9cff;--script:#a3a198;--hook:#b794ff;--step:#2dd4bf;
--add:#4ade80;--mod:#fbbf24;--del:#f87171;--plan:#7c9cff}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration-color:var(--accent);text-underline-offset:3px}
code,pre{font-family:var(--mono);font-size:.88em}
code{background:var(--soft);padding:.05em .3em}
pre{background:var(--card);border:2px solid var(--line);padding:12px 14px;overflow-x:auto}
pre code{background:none;padding:0}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
header.hero{border-bottom:3px solid var(--line);padding:40px 0 24px}
.kicker{font:600 12px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin:0 0 12px}
h1{font-size:clamp(28px,6vw,52px);line-height:1.05;margin:0 0 10px;letter-spacing:-.02em;overflow-wrap:anywhere}
.subtitle{font-size:18px;color:var(--muted);margin:0;max-width:60ch}
dl.meta{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 0}
dl.meta div{border:1.5px solid var(--line);padding:4px 10px;font:13px var(--mono);overflow-wrap:anywhere}
dl.meta dt{display:inline;color:var(--muted)}dl.meta dd{display:inline;margin:0 0 0 6px}
section{padding:28px 0;border-bottom:1px solid var(--soft)}
h2{font:700 13px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;margin:0 0 16px}
h3{font-size:18px;margin:0 0 8px}
.prose{max-width:70ch}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border:2px solid var(--line);padding:14px 16px}
.card h3{font-size:16px}.card p{margin:.3em 0 0;color:var(--muted)}
figure.diagram{margin:0 0 20px}
.canvas{overflow:auto;border:2px solid var(--line);background:var(--card);
background-image:radial-gradient(var(--soft) 1px,transparent 1px);background-size:16px 16px;max-height:80vh}
.canvas.fitted svg{width:100%;height:auto}
figcaption{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin-top:8px;color:var(--muted);font-size:14px}
.legend{display:flex;flex-wrap:wrap;gap:10px;font:12px var(--mono)}
.lg i{display:inline-block;width:10px;height:10px;margin-right:5px;background:var(--muted);vertical-align:-1px}
button.fit{font:12px var(--mono);background:none;color:var(--fg);border:1.5px solid var(--line);padding:3px 8px;cursor:pointer;margin-left:auto}
svg.dia{display:block}
.colh{font:700 11px var(--mono);letter-spacing:.12em;text-transform:uppercase;fill:var(--muted)}
.edge{fill:none;stroke:var(--muted);stroke-width:1.5;opacity:.75}
.edge.s-dashed,.edge.s-async{stroke-dasharray:5 4}
.edge.s-emphasis{stroke:var(--accent);stroke-width:2}
.ahead{fill:var(--muted)}
.elabel{font:11px var(--mono);fill:var(--muted);text-anchor:middle;paint-order:stroke;stroke:var(--card);stroke-width:4px}
.node{cursor:pointer;outline:none}
.node .box{fill:var(--card);stroke:var(--line);stroke-width:1.5}
.node .strip{fill:var(--script)}
.node .lbl{font:600 13px system-ui,sans-serif;fill:var(--fg)}
.node .sub{font:11px var(--mono);fill:var(--muted)}
.node .badge{font:700 9px var(--mono);letter-spacing:.08em;fill:var(--accent)}
.node:hover .box,.node:focus .box{stroke-width:3}
.node.c-added .box{stroke:var(--add)}.node.c-added .badge{fill:var(--add)}
.node.c-modified .box{stroke:var(--mod)}.node.c-modified .badge{fill:var(--mod)}
.node.c-removed .box{stroke:var(--del);stroke-dasharray:5 3}.node.c-removed .badge{fill:var(--del)}
.node.c-planned .box{stroke:var(--plan);stroke-dasharray:3 3}.node.c-planned .badge{fill:var(--plan)}
.k-user .strip,.lg.k-user i,.k-skill .strip,.lg.k-skill i{fill:var(--accent);background:var(--accent)}
.k-lane .strip,.lg.k-lane i,.k-module .strip,.lg.k-module i{fill:var(--lane);background:var(--lane)}
.k-hook .strip,.lg.k-hook i,.k-shell .strip,.lg.k-shell i{fill:var(--hook);background:var(--hook)}
.k-step .strip,.lg.k-step i,.k-python .strip,.lg.k-python i{fill:var(--step);background:var(--step)}
.k-javascript .strip,.lg.k-javascript i,.k-typescript .strip,.lg.k-typescript i{fill:var(--mod);background:var(--mod)}
svg.dimmed .node:not(.on){opacity:.28}svg.dimmed .edge:not(.on),svg.dimmed .elabel:not(.on){opacity:.12}
svg .edge.on{stroke:var(--accent);opacity:1;stroke-width:2.2}
.panel{position:fixed;right:16px;bottom:16px;width:min(380px,calc(100vw - 32px));max-height:60vh;overflow:auto;
background:var(--card);border:2px solid var(--line);box-shadow:6px 6px 0 var(--line);padding:16px;z-index:5}
.panel[hidden]{display:none}
.panel .close{position:absolute;top:8px;right:8px;font:16px var(--mono);background:none;border:0;color:var(--fg);cursor:pointer;padding:4px 8px}
.panel .chip{display:inline-block;font:11px var(--mono);border:1px solid var(--line);padding:1px 6px;margin:0 6px 8px 0}
.panel ul{padding-left:18px;margin:6px 0}.panel h4{font:700 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;margin:12px 0 4px;color:var(--muted)}
.panel button.jump{font:inherit;background:none;border:0;color:inherit;text-decoration:underline;text-decoration-color:var(--accent);cursor:pointer;padding:0}
table{border-collapse:collapse;width:100%;font-size:14px}
.tablewrap{overflow-x:auto}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--soft);vertical-align:top}
th{font:700 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
td.path{font-family:var(--mono);font-size:13px;overflow-wrap:anywhere}
.st{font:700 11px var(--mono);padding:1px 6px;border:1.5px solid currentColor}
.st-a,.st-pass,.lvl-low{color:var(--add)}.st-m,.st-r,.st-skip,.lvl-med{color:var(--mod)}.st-d,.st-fail,.lvl-high{color:var(--del)}
.st-unknown{color:var(--muted)}
.num{font-family:var(--mono);white-space:nowrap}.plus{color:var(--add)}.minus{color:var(--del)}
ol.list,ul.list{padding-left:22px}ol.list li,ul.list li{margin:0 0 10px}
.commit code{background:none;padding:0;color:var(--accent)}
.muted{color:var(--muted)}
footer{padding:24px 0 48px;color:var(--muted);font:12px var(--mono)}
@media (max-width:640px){header.hero{padding-top:24px}.panel{right:8px;left:8px;bottom:8px;width:auto;max-height:50vh}
th.hide-sm,td.hide-sm{display:none}}
@media (prefers-reduced-motion:no-preference){.node .box{transition:stroke-width .12s}}
"""

JS = r"""
(function(){
  var D=JSON.parse(document.getElementById('factory-data').textContent||'{}');
  var panel=document.getElementById('panel');
  function el(t,txt,cls){var e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e;}
  function clear(){document.querySelectorAll('svg.dimmed').forEach(function(s){s.classList.remove('dimmed');
    s.querySelectorAll('.on').forEach(function(x){x.classList.remove('on');});});}
  function close(){panel.hidden=true;clear();}
  function sel(svg,attr,v){return Array.prototype.filter.call(svg.querySelectorAll('['+attr+']'),function(x){return x.getAttribute(attr)===v;});}
  function open(dia,id,focusPanel){
    var d=D[dia+':'+id];if(!d)return;
    var node=Array.prototype.filter.call(document.querySelectorAll('.node'),function(n){return n.getAttribute('data-dia')===String(dia)&&n.getAttribute('data-id')===id;})[0];
    clear();
    if(node){var svg=node.ownerSVGElement;svg.classList.add('dimmed');node.classList.add('on');
      sel(svg,'data-from',id).concat(sel(svg,'data-to',id)).forEach(function(e){e.classList.add('on');
        var other=e.getAttribute('data-from')===id?e.getAttribute('data-to'):e.getAttribute('data-from');
        sel(svg,'data-id',other).forEach(function(n){n.classList.add('on');});});}
    var body=panel.querySelector('.body');body.textContent='';
    body.appendChild(el('h3',d.label));
    if(d.kind)body.appendChild(el('span',d.kind,'chip'));
    if(d.change)body.appendChild(el('span',d.change,'chip'));
    if(d.sub)body.appendChild(el('p',d.sub,'muted'));
    String(d.detail||'').split(/\n\s*\n/).forEach(function(p){if(!p.trim())return;var para=el('p');
      p.trim().split('`').forEach(function(t,i){para.appendChild(i%2?el('code',t):document.createTextNode(t));});
      body.appendChild(para);});
    if(d.sources.length){body.appendChild(el('h4','Source'));var ul=el('ul');d.sources.forEach(function(s){var li=el('li');
      if(s.href&&/^https?:\/\//.test(s.href)){var a=el('a',s.text);a.href=s.href;a.target='_blank';a.rel='noopener';li.appendChild(a);}
      else li.appendChild(el('code',s.text));ul.appendChild(li);});body.appendChild(ul);}
    [['out','Uses'],['in','Used by']].forEach(function(p){var list=d[p[0]];if(!list.length)return;
      body.appendChild(el('h4',p[1]));var ul=el('ul');list.forEach(function(x){var li=el('li');
        var b=el('button',d.names[x[0]]||x[0],'jump');b.type='button';b.onclick=function(){open(dia,x[0],true);};li.appendChild(b);
        if(x[1])li.appendChild(el('span',' — '+x[1],'muted'));ul.appendChild(li);});body.appendChild(ul);});
    panel.hidden=false;if(focusPanel)panel.querySelector('.close').focus();
  }
  document.addEventListener('click',function(ev){
    var n=ev.target.closest&&ev.target.closest('.node');
    if(n){open(n.getAttribute('data-dia'),n.getAttribute('data-id'));ev.stopPropagation();return;}
    var f=ev.target.closest&&ev.target.closest('button.fit');
    if(f){var c=f.closest('figure').querySelector('.canvas');var on=c.classList.toggle('fitted');f.setAttribute('aria-pressed',on);return;}
    if(!(ev.target.closest&&ev.target.closest('.panel')))close();
  });
  document.addEventListener('keydown',function(ev){
    if(ev.key==='Escape')close();
    var n=ev.target.closest&&ev.target.closest('.node');
    if(n&&(ev.key==='Enter'||ev.key===' ')){ev.preventDefault();open(n.getAttribute('data-dia'),n.getAttribute('data-id'),true);}
  });
  panel.querySelector('.close').addEventListener('click',close);
})();
"""

KICKER = {"recap": "Recap", "plan": "Plan", "map": "Codebase map", "skillmap": "Skill map"}


def link_or_code(path: str, base: str | None) -> str:
    text, href = source_link(path, base)
    return '<a href="%s">%s</a>' % (esc(href), esc(text)) if href else esc(text)


def section(title: str, inner: str, cls: str = "") -> str:
    return '<section class="%s"><div class="wrap"><h2>%s</h2>%s</div></section>' % (esc(cls), esc(title), inner)


def flow_from_steps(steps: list) -> dict:
    nodes, edges = [], []
    for i, s in enumerate(steps):
        files = s.get("files") or []
        nodes.append({"id": "step-%d" % (i + 1), "label": "%d. %s" % (i + 1, s.get("title", "")), "kind": "step",
                      "sub": ("%d file%s" % (len(files), "" if len(files) == 1 else "s")) if files else "",
                      "detail": s.get("detail", ""), "source": files})
        if i:
            edges.append({"from": "step-%d" % i, "to": "step-%d" % (i + 1)})
    return {"title": "Flow", "nodes": nodes, "edges": edges}


def render_page(spec: dict, base: str | None = None) -> str:
    base = check_base(base)
    if not isinstance(spec, dict) or not spec.get("title"):
        raise SystemExit("visual: spec needs at least a title")
    kind = str(spec.get("kind") or "recap")
    out = []
    meta = spec.get("meta") or {}
    meta_html = "".join("<div><dt>%s</dt><dd>%s</dd></div>" % (esc(k), esc(v)) for k, v in meta.items())
    out.append('<header class="hero"><div class="wrap"><p class="kicker">%s</p><h1>%s</h1>%s%s</div></header>' % (
        esc(spec.get("kicker") or KICKER.get(kind, kind)), esc(spec["title"]),
        '<p class="subtitle">%s</p>' % esc(spec["subtitle"]) if spec.get("subtitle") else "",
        '<dl class="meta">%s</dl>' % meta_html if meta_html else ""))
    out.append("<main>")
    if spec.get("summary"):
        out.append(section("Summary", '<div class="prose">%s</div>' % md(spec["summary"])))
    diagrams = list(spec.get("diagrams") or [])
    if kind == "plan" and not diagrams and spec.get("steps"):
        diagrams = [flow_from_steps(spec["steps"])]
    details: dict = {}
    if diagrams:
        figs = "".join(render_diagram(d, i, base, details) for i, d in enumerate(diagrams))
        out.append(section("Diagram" if len(diagrams) == 1 else "Diagrams",
                           figs + '<p class="muted">Click or tab to a box for details and source links.</p>'))
    if spec.get("highlights"):
        out.append(section("Highlights", '<div class="cards">%s</div>' % "".join(
            '<div class="card"><h3>%s</h3><p>%s</p></div>' % (esc(h.get("title")), inline_md(h.get("detail", "")))
            for h in spec["highlights"])))
    if spec.get("steps"):
        items = []
        for s in spec["steps"]:
            files = ", ".join(link_or_code(f, base) for f in s.get("files") or [])
            items.append("<li><strong>%s</strong>%s%s</li>" % (
                esc(s.get("title")), "<br>" + inline_md(s["detail"]) if s.get("detail") else "",
                '<br><span class="muted">%s</span>' % files if files else ""))
        out.append(section("Steps", '<ol class="list">%s</ol>' % "".join(items)))
    if spec.get("commits"):
        cb = commit_base(base)
        rows = []
        for c in spec["commits"]:
            sha = str(c.get("sha", ""))[:12]
            sha_html = ('<a href="%s"><code>%s</code></a>' % (esc(cb + quote(sha)), esc(sha))
                        if cb and re.match(r"^[0-9a-f]{4,40}$", sha) else "<code>%s</code>" % esc(sha))
            rows.append('<li class="commit">%s %s%s</li>' % (sha_html, esc(c.get("subject")),
                        ' <span class="muted">— %s</span>' % esc(c["author"]) if c.get("author") else ""))
        out.append(section("Commits (%d)" % len(spec["commits"]), '<ul class="list">%s</ul>' % "".join(rows)))
    if spec.get("files"):
        rows, ta, tr = [], 0, 0
        for f in spec["files"]:
            st = css_token(f.get("status") or "m")[:1] or "m"
            a, r = f.get("added"), f.get("removed")
            ta += a if isinstance(a, int) else 0
            tr += r if isinstance(r, int) else 0
            num = ('<span class="plus">+%s</span> <span class="minus">−%s</span>' % (esc(a), esc(r))
                   if a is not None or r is not None else "")
            rows.append('<tr><td><span class="st st-%s">%s</span></td><td class="path">%s%s</td><td class="num">%s</td></tr>' % (
                st, esc(st.upper()), link_or_code(f.get("path", ""), base),
                '<br><span class="muted">%s</span>' % inline_md(f["note"]) if f.get("note") else "", num))
        total = '<p class="muted num">%d files · <span class="plus">+%d</span> <span class="minus">−%d</span></p>' % (
            len(spec["files"]), ta, tr)
        out.append(section("Files", total + '<div class="tablewrap"><table><thead><tr><th>St</th><th>Path</th>'
                           '<th>Lines</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows)))
    if spec.get("risks"):
        out.append(section("Risks", '<ul class="list">%s</ul>' % "".join(
            '<li><span class="st lvl-%s">%s</span> %s</li>' % (
                css_token(r.get("level") or "med"), esc(str(r.get("level") or "med").upper()), inline_md(r.get("text", "")))
            for r in spec["risks"])))
    if spec.get("tests"):
        rows = []
        for t in spec["tests"]:
            st = css_token(t.get("status") or "unknown")
            rows.append("<tr><td><span class=\"st st-%s\">%s</span></td><td>%s%s%s</td></tr>" % (
                st, esc(st.upper()), esc(t.get("name")),
                "<br><code>%s</code>" % esc(t["cmd"]) if t.get("cmd") else "",
                '<br><span class="muted">%s</span>' % inline_md(t["detail"]) if t.get("detail") else ""))
        out.append(section("Tests", '<div class="tablewrap"><table><tbody>%s</tbody></table></div>' % "".join(rows)))
    if spec.get("questions"):
        out.append(section("Open questions", '<ol class="list">%s</ol>' % "".join(
            "<li>%s%s</li>" % (inline_md(q.get("q", "")),
                               '<br><span class="muted">Default: %s</span>' % inline_md(q["default"]) if q.get("default") else "")
            for q in spec["questions"])))
    for s in spec.get("sections") or []:
        out.append(section(s.get("title") or "Notes", '<div class="prose">%s</div>' % md(s.get("body", ""))))
    out.append("</main>")
    out.append('<footer><div class="wrap">factory /visual%s</div></footer>' % (
        " · " + esc(spec["generated"]) if spec.get("generated") else ""))
    out.append('<aside class="panel" id="panel" hidden aria-live="polite"><button type="button" class="close" '
               'aria-label="Close">×</button><div class="body"></div></aside>')
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="color-scheme" content="light dark">\n<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n%s\n'
            '<script type="application/json" id="factory-data">%s</script>\n<script>%s</script>\n</body>\n</html>\n') % (
        esc(spec["title"]), CSS.strip(), "\n".join(out), safe_json(details), JS.strip())


# ---------------------------------------------------------------- git facts (recap)


def git(*args, cwd=None, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or "git %s failed" % " ".join(args))
    return p.stdout


def repo_root(start: str = ".") -> str:
    try:
        return git("rev-parse", "--show-toplevel", cwd=start).strip()
    except (RuntimeError, OSError):
        return os.path.abspath(start)


def default_base(root: str) -> str:
    cfg = os.environ.get("FACTORY_CONFIG") or os.path.join(root, ".claude", "factory.json")
    try:
        with open(cfg) as fh:
            b = json.load(fh).get("base_branch")
    except (OSError, ValueError):
        b = None
    cands = ([b] if b else []) + ["main", "master"]
    for c in cands:
        for ref in ("origin/" + c, c):
            if subprocess.run(["git", "rev-parse", "--verify", "-q", ref + "^{commit}"], cwd=root,
                              capture_output=True).returncode == 0:
                return ref
    return "HEAD"


def module_of(path: str) -> str:
    parts = path.split("/")
    if len(parts) == 1:
        return "(root)"
    if parts[0] in ("skills", "packages", "apps", "services", "src", "lib", "cmd", "internal") and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


SENSITIVE = [
    (r"(^|/)\.github/workflows/", "high", "CI/CD workflow changed; check permissions and secrets use"),
    (r"(auth|login|session|token|secret|credential|password|permission|iam|rbac)", "high", "touches auth or secrets"),
    (r"(migrat|schema\.(sql|prisma)|\.sql$)", "high", "database migration or schema"),
    (r"(payment|billing|stripe|invoice)", "high", "payments or billing"),
    (r"(\.tf$|terraform|helm|k8s|kustomiz|dockerfile)", "med", "infrastructure or deploy config"),
    (r"(package-lock\.json|yarn\.lock|pnpm-lock|poetry\.lock|requirements.*\.txt|go\.sum|Gemfile\.lock)$", "med",
     "dependency versions changed"),
]


def risk_hints(files: list) -> list:
    hints = []
    for rx, level, why in SENSITIVE:
        hit = [f["path"] for f in files if re.search(rx, f["path"], re.I)]
        if hit:
            hints.append({"level": level, "text": "%s: %s" % (why, ", ".join("`%s`" % h for h in hit[:5])) +
                          (" (+%d more)" % (len(hit) - 5) if len(hit) > 5 else "")})
    code = [f for f in files if re.search(r"\.(py|sh|js|ts|tsx|jsx|go|rb|rs|java|kt|dart|swift)$", f["path"])]
    tests = [f for f in files if re.search(r"(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|\.spec\.|test_", f["path"])]
    if code and not tests:
        hints.append({"level": "med", "text": "code changed but no test files touched"})
    big = [f for f in files if (f.get("removed") or 0) >= 200]
    if big:
        hints.append({"level": "low", "text": "large deletions in %s" % ", ".join("`%s`" % f["path"] for f in big[:5])})
    return hints


def recap_facts(root: str, base: str | None = None) -> dict:
    base = base or default_base(root)
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=root).strip()
    mb = git("merge-base", base, "HEAD", cwd=root).strip() if base != "HEAD" else git("rev-parse", "HEAD", cwd=root).strip()
    commits = []
    for line in git("log", "--no-merges", "--format=%h%x09%s%x09%an", "%s..HEAD" % mb, cwd=root).splitlines():
        sha, subj, author = (line.split("\t") + ["", ""])[:3]
        commits.append({"sha": sha, "subject": subj, "author": author})
    stats = {}
    for line in git("diff", "--numstat", "-M", mb, cwd=root).splitlines():
        a, r, path = line.split("\t", 2)
        if " => " in path:
            path = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", path).replace("//", "/")
            path = path.split(" => ")[-1]
        stats[path] = (int(a) if a.isdigit() else None, int(r) if r.isdigit() else None)
    files = []
    for line in git("diff", "--name-status", "-M", mb, cwd=root).splitlines():
        cols = line.split("\t")
        st, path = cols[0][:1], cols[-1]
        a, r = stats.get(path, (None, None))
        files.append({"path": path, "status": st, "added": a, "removed": r})
    for path in git("ls-files", "--others", "--exclude-standard", cwd=root).splitlines():
        try:
            with open(os.path.join(root, path), "rb") as fh:
                n = fh.read().count(b"\n")
        except OSError:
            n = None
        files.append({"path": path, "status": "A", "added": n, "removed": 0, "note": "untracked"})
    files.sort(key=lambda f: f["path"])
    mods = {}
    for f in files:
        mods.setdefault(module_of(f["path"]), []).append(f)
    nodes = []
    for m, fs in sorted(mods.items()):
        sts = {f["status"] for f in fs}
        change = "added" if sts == {"A"} else "removed" if sts == {"D"} else "modified"
        add = sum(f.get("added") or 0 for f in fs)
        rem = sum(f.get("removed") or 0 for f in fs)
        nodes.append({"id": m, "label": m, "sub": "%d file%s · +%d −%d" % (len(fs), "" if len(fs) == 1 else "s", add, rem),
                      "kind": "module", "change": change, "detail": "\n\n".join(f["path"] for f in fs),
                      "source": [f["path"] for f in fs if f["status"] != "D"][:12]})
    edges = module_edges(root, [f["path"] for f in files if f["status"] != "D"], set(mods))
    try:
        with open(os.environ.get("FACTORY_CONFIG") or os.path.join(root, ".claude", "factory.json")) as fh:
            verify = json.load(fh).get("verify_commands") or []
    except (OSError, ValueError):
        verify = []
    dirty = bool(git("status", "--porcelain", cwd=root).strip())
    return {
        "kind": "recap", "title": "Recap: %s" % branch, "subtitle": "",
        "meta": {"branch": branch, "base": base, "merge-base": mb[:12], "commits": str(len(commits)),
                 "uncommitted": "yes" if dirty else "no"},
        "summary": "", "commits": commits, "files": files,
        "diagrams": [{"title": "Touched modules", "caption": "Modules changed on this branch; arrows are imports between them.",
                      "nodes": nodes, "edges": edges}] if nodes else [],
        "risks": risk_hints(files),
        "tests": [{"name": c, "cmd": c, "status": "unknown"} for c in verify],
        "slug": "recap-" + slugify(branch),
    }


def module_edges(root: str, paths: list, modules: set) -> list:
    graph = import_graph(root, [os.path.join(root, p) for p in paths], root)
    seen, edges = set(), []
    for a, b in graph:
        ma, mb = module_of(a), module_of(b)
        if ma != mb and ma in modules and mb in modules and (ma, mb) not in seen:
            seen.add((ma, mb))
            edges.append({"from": ma, "to": mb, "label": "imports"})
    return edges


# ---------------------------------------------------------------- codebase scan (map)

CODE_EXT = {".py": "python", ".sh": "shell", ".bash": "shell", ".js": "javascript", ".mjs": "javascript",
            ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "_site", ".next", "coverage",
             ".claude", ".mypy_cache", ".pytest_cache", "vendor", "target"}
JS_EXT = [".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx"]


def code_files(top: str) -> list:
    out = []
    for d, dirs, files in os.walk(top):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for f in sorted(files):
            if os.path.splitext(f)[1] in CODE_EXT or (not os.path.splitext(f)[1] and _shebang(os.path.join(d, f))):
                out.append(os.path.join(d, f))
    return out


def _shebang(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def lang_of(path: str, text: str = "") -> str:
    ext = os.path.splitext(path)[1]
    if ext in CODE_EXT:
        return CODE_EXT[ext]
    first = text.split("\n", 1)[0]
    return "python" if "python" in first else "javascript" if "node" in first else "shell"


def describe(text: str, lang: str) -> str:
    """First docstring or header comment line, minus the 'name — ' prefix."""
    if lang == "python":
        m = re.search(r'^(?:#[^\n]*\n|\s*\n)*\s*(?:"""|\'\'\')\s*(.+)', text)
        line = m.group(1) if m else ""
    else:
        line = ""
        for ln in text.split("\n")[:15]:
            s = ln.strip()
            if s.startswith("#!") or not s:
                continue
            if s.startswith(("#", "//", "*", "/*")):
                line = s.lstrip("#/* ").strip()
                if line:
                    break
            else:
                break
    line = re.sub(r'("""|\'\'\').*', "", line).strip()
    return re.sub(r"^[\w.-]+\s+[—-]+\s+", "", line)


def _resolve_js(base_dir: str, spec: str, known: set):
    p = os.path.normpath(os.path.join(base_dir, spec))
    for cand in [p] + [p + e for e in JS_EXT] + [os.path.join(p, "index" + e) for e in JS_EXT]:
        if cand in known:
            return cand
    return None


def _resolve_py(fdir: str, mod: str, level: int, roots: list, known: set):
    rel = mod.replace(".", "/") if mod else ""
    bases = [fdir] if level == 0 else [os.path.normpath(os.path.join(fdir, *([".."] * (level - 1))))]
    if level == 0:
        bases += roots
    for b in bases:
        p = os.path.join(b, rel) if rel else b
        for cand in (p + ".py", os.path.join(p, "__init__.py")):
            if cand in known:
                return cand
    return None


def import_graph(top: str, files: list, rel_to: str) -> list:
    """(from, to) pairs between files, relative to rel_to. Python imports, JS/TS relative
    imports, and sibling scripts named in code (board.sh, jev.py)."""
    known = set(os.path.abspath(f) for f in code_files(top))
    by_dir = {}
    for k in known:
        by_dir.setdefault(os.path.dirname(k), {})[os.path.basename(k)] = k
    roots = [os.path.abspath(top)]
    pairs = []
    for f in files:
        f = os.path.abspath(f)
        if f not in known:
            continue
        text = _read(f)
        lang = lang_of(f, text)
        fdir = os.path.dirname(f)
        targets = []
        if lang == "python":
            for m in re.finditer(r"^\s*from\s+(\.*)([\w.]*)\s+import\s+\(?\s*([\w, \n]*)", text, re.M):
                level, mod = len(m.group(1)), m.group(2)
                hit = _resolve_py(fdir, mod, level, roots, known)
                if hit and hit.endswith("__init__.py") and level:
                    for name in m.group(3).split(","):
                        sub = _resolve_py(fdir, (mod + "." if mod else "") + name.strip(), level, roots, known)
                        targets.append(sub or hit)
                else:
                    targets.append(hit)
            for m in re.finditer(r"^\s*import\s+([\w., ]+)", text, re.M):
                for mod in m.group(1).split(","):
                    targets.append(_resolve_py(fdir, mod.strip().split(" ")[0], 0, roots, known))
        if lang in ("javascript", "typescript"):
            for m in re.finditer(r"""(?:from\s+|import\s*\(?\s*|require\(\s*)['"](\.{1,2}/[^'"]+)['"]""", text):
                targets.append(_resolve_js(fdir, m.group(1), known))
        code = "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith(("#", "//")))
        for name, path in by_dir.get(fdir, {}).items():
            n = re.escape(name)
            # a real call, not a mention: a quoted file name ("board.sh", '/jev.py') in Python/JS,
            # or a path ending in the name ($here/jev.py, ./board.sh) in shell
            rx = (r"""["'](?:[^"'\s]*/)?%s["']""" % n if lang != "shell"
                  else r"""(?:\$\{?\w+\}?|\)|\.)/%s(?![\w-])""" % n)
            if path != f and re.search(rx, code):
                targets.append(path)
        for t in targets:
            if t and t != f:
                pair = (os.path.relpath(f, rel_to), os.path.relpath(t, rel_to))
                if pair not in pairs:
                    pairs.append(pair)
    return pairs


def scan(target: str, root: str, max_nodes: int = 60) -> dict:
    target = os.path.abspath(target)
    files = code_files(target)
    rel = lambda p: os.path.relpath(p, root).replace(os.sep, "/")
    pairs = import_graph(target, files, root)
    nodes, edges = [], []
    if len(files) <= max_nodes:
        for f in files:
            text = _read(f)
            lang = lang_of(f, text)
            lines = text.count("\n") or 1
            r = rel(f)
            d = os.path.dirname(os.path.relpath(f, target)).replace(os.sep, "/")
            nodes.append({"id": r, "label": os.path.basename(f), "sub": "%s%d lines" % (d + "/ · " if d else "", lines),
                          "kind": lang, "detail": describe(text, lang), "source": "%s:1-%d" % (r, lines)})
        edges = [{"from": a, "to": b} for a, b in pairs]
    else:  # too many files: one node per directory
        groups = {}
        for f in files:
            d = os.path.dirname(os.path.relpath(f, target)).replace(os.sep, "/") or "."
            groups.setdefault(d.split("/")[0] if d != "." else ".", []).append(f)
        gof = {rel(f): g for g, fs in groups.items() for f in fs}
        for g, fs in sorted(groups.items()):
            langs = sorted({lang_of(f) for f in fs})
            nodes.append({"id": g, "label": g if g != "." else os.path.basename(target) or ".",
                          "sub": "%d files · %s" % (len(fs), "/".join(langs)), "kind": "module",
                          "detail": "\n\n".join(rel(f) for f in fs[:30]) + ("\n\n…" if len(fs) > 30 else ""),
                          "source": rel(os.path.join(target, g)) if g != "." else rel(target)})
        seen = set()
        for a, b in pairs:
            ga, gb = gof.get(a), gof.get(b)
            if ga and gb and ga != gb and (ga, gb) not in seen:
                seen.add((ga, gb))
                edges.append({"from": ga, "to": gb})
    name = rel(target) if rel(target) != "." else os.path.basename(root)
    return {"kind": "map", "title": "Map of %s" % name, "subtitle": "", "summary": "",
            "meta": {"path": rel(target), "files": str(len(files)), "edges": str(len(edges))},
            "diagrams": [{"title": "Modules and imports", "caption": "Arrows point from a file to what it imports or runs.",
                          "nodes": nodes, "edges": edges}],
            "highlights": [], "slug": "map-" + slugify(name)}


# ---------------------------------------------------------------- CLI


def default_out(root: str, spec: dict) -> str:
    return os.path.join(root, ".claude", "factory", "visual", slugify(spec.get("slug") or spec.get("title", "page")) + ".html")


def write_page(path: str, page: str) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = page.encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return len(data)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="visual.py", description="Render factory /visual pages.")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("facts", help="recap spec skeleton for the current branch")
    f.add_argument("--base")
    s = sub.add_parser("scan", help="codebase-map spec for a directory")
    s.add_argument("dir")
    s.add_argument("--max-nodes", type=int, default=60)
    r = sub.add_parser("render", help="spec JSON -> HTML page")
    r.add_argument("spec", help="spec file, or - for stdin")
    r.add_argument("--out")
    r.add_argument("--source-base", help="URL prefix for source links, e.g. https://github.com/O/R/blob/main/")
    a = p.parse_args(argv)
    root = repo_root()
    if a.cmd == "facts":
        try:
            print(json.dumps(recap_facts(root, a.base), indent=2, ensure_ascii=False))
        except RuntimeError as e:
            print("visual: %s" % e, file=sys.stderr)
            return 1
        return 0
    if a.cmd == "scan":
        if not os.path.isdir(a.dir):
            print("visual: not a directory: %s" % a.dir, file=sys.stderr)
            return 1
        print(json.dumps(scan(a.dir, root, a.max_nodes), indent=2, ensure_ascii=False))
        return 0
    if a.spec == "-":
        spec = json.load(sys.stdin)
    else:
        with open(a.spec, encoding="utf-8") as fh:
            spec = json.load(fh)
    out = a.out or default_out(root, spec)
    n = write_page(out, render_page(spec, a.source_base))
    print(json.dumps({"out": out, "bytes": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
