#!/usr/bin/env python3
"""readme-sync.py — keep README.md's skills table in step with the skills.

The table between <!-- skills:start --> and <!-- skills:end --> is generated
from each skills/*/SKILL.md `name` and `description`. With skills/families.json
it is split into groups, in order:

  {"groups": [{"title": "You type", "skills": {"board": "", "collect": "Short brief"}},
              {"title": "The board runs", "skills": {"build": "", "qa": "", "review": ""}}]}

(a plain {"<id>": {"title": ..., "skills": {...}}} map works too; other keys are ignored).
An empty brief uses the description's first sentence. Skills missing from
families.json go in a final "Other" group, so a new skill is never dropped.

Usage:
  readme-sync.py [--root DIR]          rewrite README.md if it is stale
  readme-sync.py --check [--root DIR]  exit 1 if stale, write nothing (CI)
  readme-sync.py --hook                PostToolUse: payload on stdin; when a
                                       <root>/skills/<name>/SKILL.md was written
                                       in a plugin repo (<root>/.claude-plugin/
                                       plugin.json), sync <root>/README.md.
                                       Always exits 0.

Exit codes: 0 up to date or rewritten, 1 stale (--check), 2 bad input
(no markers, unreadable families.json, a skill with no description).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

START, END = "<!-- skills:start -->", "<!-- skills:end -->"
DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SyncError(Exception):
    pass


def frontmatter(path: str) -> Dict[str, str]:
    """`key: value` pairs, with `>`/`|` blocks and quoted values folded to one line."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    out: Dict[str, str] = {}
    if not m:
        return out
    key: Optional[str] = None
    for line in m.group(1).splitlines():
        kv = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv and not line[:1].isspace():
            key, value = kv.group(1), kv.group(2).strip()
            if value in (">", ">-", "|", "|-"):
                out[key] = ""
            else:
                out[key] = value.strip("\"'")
                key = None
        elif key is not None and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def brief(description: str) -> str:
    return re.split(r"(?<=[.!?])\s", description.strip(), 1)[0]


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(root: str) -> str:
    skills_dir = os.path.join(root, "skills")
    found: Dict[str, Dict[str, str]] = {}
    for name in sorted(os.listdir(skills_dir)) if os.path.isdir(skills_dir) else []:
        md = os.path.join(skills_dir, name, "SKILL.md")
        if os.path.isfile(md):
            fm = frontmatter(md)
            if not fm.get("description"):
                raise SyncError(f"skills/{name}/SKILL.md has no description")
            found[name] = fm

    groups: List[Tuple[Optional[str], List[Tuple[str, str]]]] = []
    families_path = os.path.join(skills_dir, "families.json")
    if os.path.exists(families_path):
        try:
            with open(families_path, encoding="utf-8") as fh:
                families = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SyncError(f"cannot read skills/families.json: {exc}")
        # Either {"groups": [{title, skills}]} or {"<id>": {title, skills}}.
        fams = families.get("groups") if isinstance(families, dict) else None
        if not isinstance(fams, list):
            fams = list(families.values()) if isinstance(families, dict) else []
        listed = set()
        for fam in fams:
            if not isinstance(fam, dict) or not isinstance(fam.get("skills"), dict):
                continue
            rows = []
            for skill, text in (fam.get("skills") or {}).items():
                if skill in found:
                    listed.add(skill)
                    rows.append((skill, text or brief(found[skill]["description"])))
            groups.append((fam.get("title"), rows))
        rest = [(s, brief(fm["description"])) for s, fm in found.items() if s not in listed]
        if rest:
            groups.append(("Other", rest))
    else:
        groups.append((None, [(s, brief(fm["description"])) for s, fm in found.items()]))

    parts: List[str] = []
    for title, rows in groups:
        if not rows:
            continue
        if title:
            parts += [f"**{title}**", ""]
        parts += ["| Skill | What it does |", "|---|---|"]
        for skill, text in rows:
            name = found[skill].get("name") or skill
            parts.append(f"| [`{name}`](skills/{skill}/SKILL.md) | {cell(text)} |")
        parts.append("")
    return "\n".join(parts)


def synced(root: str, text: str) -> str:
    if START not in text or END not in text or text.index(START) > text.index(END):
        raise SyncError(f"README.md needs {START} and {END} markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n{render(root)}{END}{tail}"


def sync(root: str, check: bool) -> int:
    readme = os.path.join(root, "README.md")
    try:
        with open(readme, encoding="utf-8") as fh:
            current = fh.read()
        fresh = synced(root, current)
    except (OSError, SyncError) as exc:
        print(f"readme-sync: {exc}", file=sys.stderr)
        return 2
    if fresh == current:
        return 0
    if check:
        print("readme-sync: README.md skills table is stale; run scripts/readme-sync.py", file=sys.stderr)
        return 1
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(fresh)
    print("readme-sync: README.md updated")
    return 0


def hook_root(payload: dict) -> Optional[str]:
    """Repo root for an edited <root>/skills/<name>/SKILL.md, if its README has markers."""
    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if os.path.basename(path) != "SKILL.md":
        return None
    if not os.path.isabs(path):
        path = os.path.join(payload.get("cwd") or os.getcwd(), path)
    skills = os.path.dirname(os.path.dirname(os.path.realpath(path)))
    if os.path.basename(skills) != "skills":
        return None
    root = os.path.dirname(skills)
    # Only plugin repos: an installed factory must not rewrite users' READMEs.
    if not os.path.isfile(os.path.join(root, ".claude-plugin", "plugin.json")):
        return None
    try:
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            return root if START in fh.read() else None
    except OSError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate README.md's skills table.")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    args = ap.parse_args(argv)
    if args.hook:
        try:
            root = hook_root(json.load(sys.stdin))
            if root:
                sync(root, check=False)
        except Exception:  # a hook never fails the edit it follows
            pass
        return 0
    return sync(os.path.abspath(args.root), args.check)


if __name__ == "__main__":
    sys.exit(main())
