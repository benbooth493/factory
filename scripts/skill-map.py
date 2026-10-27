#!/usr/bin/env python3
"""skill-map.py — build factory's skill map from the files on disk.

Usage (from the repo root):

  skill-map.py [--root DIR] [--json]  [--out FILE] [--source-base URL]

Nodes are the skills (skills/*/SKILL.md), the scripts (scripts/*) and the
hooks (hooks/hooks.json and the files it runs). Edges are found in the
text, not typed in: a SKILL.md that names `scripts/board.sh`, `board.sh`,
`skills/build/` or `/factory:collect` uses that script or skill, and
scripts call each other the way visual.py's import scan sees it.
skills/families.json says which skills you type and which the board runs;
a skill it doesn't list is placed by its description ("not for direct use"
means the board runs it). --json prints the spec; otherwise the page is
rendered with visual.py. Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_visual():
    spec = importlib.util.spec_from_file_location("factory_visual", os.path.join(HERE, "visual.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


visual = _load_visual()

YOU_TYPE, BOARD_RUNS = "you-type", "board-runs"


def frontmatter(text: str) -> dict:
    m = re.match(r"---\s*\n(.*?)\n---\s*\n", text, re.S)
    out = {}
    if not m:
        return out
    for line in m.group(1).split("\n"):
        k = re.match(r"([A-Za-z][\w-]*):\s*(.*)$", line)
        if k:
            v = k.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] == '"':
                v = re.sub(r'\\(["\\])', r"\1", v[1:-1])
            elif len(v) >= 2 and v[0] == v[-1] == "'":
                v = v[1:-1].replace("''", "'")
            out[k.group(1)] = v
    return out


def first_sentence(s: str) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", s.strip(), re.S)
    return (m.group(1) if m else s).strip()


def load_families(root: str) -> dict:
    path = os.path.join(root, "skills", "families.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"groups": []}


def read_skills(root: str, fam: dict) -> list:
    """[{name, group, blurb, description, hint, path, text}] in families order, then the rest by name."""
    where = {}
    for g in fam.get("groups", []):
        for name, blurb in (g.get("skills") or {}).items():
            where[name] = (g.get("id"), blurb)
    found = {}
    sk = os.path.join(root, "skills")
    for d in sorted(os.listdir(sk)) if os.path.isdir(sk) else []:
        md = os.path.join(sk, d, "SKILL.md")
        if not os.path.isfile(md):
            continue
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        fm = frontmatter(text)
        name = fm.get("name") or d
        desc = fm.get("description", "")
        group, blurb = where.get(name, (None, ""))
        if not group:
            group = BOARD_RUNS if re.search(r"not for direct use|run by the factory orchestrator", desc, re.I) else YOU_TYPE
        found[name] = {"name": name, "dir": d, "group": group, "blurb": blurb or first_sentence(desc),
                       "description": desc, "hint": fm.get("argument-hint", ""),
                       "path": "skills/%s/SKILL.md" % d, "text": text}
    order = [n for g in fam.get("groups", []) for n in (g.get("skills") or {}) if n in found]
    return [found[n] for n in order] + [found[n] for n in sorted(found) if n not in order]


def list_scripts(root: str, hidden: set) -> list:
    d = os.path.join(root, "scripts")
    out = []
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        p = os.path.join(d, f)
        rel = "scripts/" + f
        if os.path.isfile(p) and not f.startswith((".", "_")) and rel not in hidden and \
                (os.path.splitext(f)[1] in visual.CODE_EXT or visual._shebang(p)):
            out.append(p)
    return out


def hook_files(root: str) -> list:
    """Files hooks/hooks.json runs, in order, plus the hook modules they import."""
    path = os.path.join(root, "hooks", "hooks.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return []
    events = []
    for event, entries in (cfg.get("hooks") or {}).items():
        for e in entries:
            for h in e.get("hooks", []):
                for m in re.finditer(r"hooks/([\w.-]+)", h.get("command", "")):
                    events.append((m.group(1), event, e.get("matcher", "*")))
    return events


def build_spec(root: str) -> dict:
    root = os.path.abspath(root)
    fam = load_families(root)
    hidden = set(fam.get("hidden") or [])
    skills = read_skills(root, fam)
    names = {s["name"] for s in skills}
    scripts = list_scripts(root, hidden)
    script_names = {os.path.basename(p): "scripts/" + os.path.basename(p) for p in scripts}
    nodes, edges = [], []

    def edge(a, b, label=""):
        if a != b and not any(e["from"] == a and e["to"] == b for e in edges):
            edges.append({"from": a, "to": b, "label": label})

    for s in skills:
        lane = s["group"] == BOARD_RUNS
        label = s["name"] if lane else "/factory:%s" % s["name"]
        detail = s["blurb"]
        use = re.search(r"\bUse (?:for|when) (.*)$", s["description"], re.S)
        if use:
            detail += "\n\nUse for %s" % use.group(1).strip()
        if s["hint"]:
            detail += "\n\nArguments: `%s`" % s["hint"]
        nodes.append({"id": "skill:" + s["name"], "label": label, "kind": "lane" if lane else "skill",
                      "sub": "board lane" if lane else (s["hint"] or "skill"), "detail": detail,
                      "source": s["path"], "col": 1 if lane else 0})
    for s in skills:
        text = s["text"]
        for m in re.finditer(r"skills/\{([\w,\s-]+)\}", text):
            for n in m.group(1).split(","):
                if n.strip() in names:
                    edge("skill:" + s["name"], "skill:" + n.strip(), "runs")
        for m in re.finditer(r"(?:skills/([\w-]+)/|/factory:([\w-]+))", text):
            n = m.group(1) or m.group(2)
            if n in names:
                edge("skill:" + s["name"], "skill:" + n, "reads" if m.group(1) else "")
        for fname, rel in script_names.items():
            if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(fname), text):
                edge("skill:" + s["name"], "script:" + rel, "")
    for p in scripts:
        rel = "scripts/" + os.path.basename(p)
        text = visual._read(p)
        lang = visual.lang_of(p, text)
        lines = text.count("\n") or 1
        nodes.append({"id": "script:" + rel, "label": os.path.basename(p), "kind": "script",
                      "sub": "%s · %d lines" % (lang, lines), "detail": visual.describe(text, lang),
                      "source": "%s:1-%d" % (rel, lines), "col": 2})
    script_ids = {"script:scripts/" + os.path.basename(p) for p in scripts}
    for a, b in visual.import_graph(os.path.join(root, "scripts"), scripts, root):
        if "script:" + a in script_ids and "script:" + b in script_ids:
            edge("script:" + a, "script:" + b, "calls")
    hooks = hook_files(root)
    if hooks:
        nodes.append({"id": "hook:hooks.json", "label": "hooks.json", "kind": "hook", "sub": "fires on every tool call",
                      "detail": "Claude Code runs these before each matching tool call. A block exits 2 and tells "
                                "Claude why, so it can correct course.", "source": "hooks/hooks.json", "col": 3})
    seen = set()
    for fname, event, matcher in hooks:
        if fname in seen:
            continue
        seen.add(fname)
        p = os.path.join(root, "hooks", fname)
        text = visual._read(p)
        lang = visual.lang_of(p, text)
        nodes.append({"id": "hook:" + fname, "label": fname, "kind": "hook", "sub": "%s · %s" % (event, matcher),
                      "detail": visual.describe(text, lang), "source": "hooks/" + fname, "col": 4})
        edge("hook:hooks.json", "hook:" + fname, matcher)
    return {
        "kind": "skillmap", "title": "factory skill map",
        "subtitle": "Every skill, script and hook, and what uses what. Built from the files in the repo.",
        "meta": {"skills": str(len(skills)), "scripts": str(len(scripts)), "hooks": str(len(seen)),
                 "links": str(len(edges))},
        "diagrams": [{"title": "Skills → scripts → hooks",
                      "caption": "Click a box for what it does, its source, and what it uses.",
                      "columns": ["You type", "The board runs", "Scripts", "Hooks", ""], "align": "top",
                      "nodes": nodes, "edges": edges}],
        "slug": "skill-map",
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="skill-map.py", description=__doc__.split("\n")[0])
    p.add_argument("--root", default=visual.repo_root(HERE))
    p.add_argument("--json", action="store_true", help="print the spec instead of rendering")
    p.add_argument("--out")
    p.add_argument("--source-base")
    a = p.parse_args(argv)
    spec = build_spec(a.root)
    if a.json:
        print(json.dumps(spec, indent=2, ensure_ascii=False))
        return 0
    out = a.out or visual.default_out(a.root, spec)
    n = visual.write_page(out, visual.render_page(spec, a.source_base))
    print(json.dumps({"out": out, "bytes": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
