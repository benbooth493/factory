#!/usr/bin/env python3
"""skills-ready.py — are the skills factory's lanes lean on actually installed?

factory requires three outside skill packs: Matt Pocock's skills, impeccable
and humanizer. A lane that reaches for a missing skill improvises instead, so
the board checks first and stops with install instructions. Read-only: this
never installs anything.

Usage:
  skills-ready.py --all                     every lane
  skills-ready.py --lane build --kind bug   one lane (kind: bug|feature|refactor)
  skills-ready.py --lane build              build for any kind
  skills-ready.py --skill mattpocock-skills:grilling   one named skill
  add --json for machine output; --home / --project to point at other dirs (tests)

Exit codes: 0 every required skill resolves, 10 something required is
missing (install instructions on stderr), 2 bad usage.

Where Claude Code finds skills, and so where this looks:
  plain name `tdd`            <project>/.claude/skills/tdd/SKILL.md, then ~/.claude/skills/tdd/SKILL.md
  plugin name `pack:skill`    an installed, enabled plugin called `pack`
                              (~/.claude/plugins/installed_plugins.json → installPath),
                              in its skills/ dir or the dirs its plugin.json `skills`
                              names; else a bare `skill` folder in the dirs above
                              (where `npx skills add` puts it)
A skill counts only when its SKILL.md is a readable, non-empty file. A plugin
switched off in settings.json `enabledPlugins` doesn't count.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Tuple

M = "mattpocock-skills:"

# lane -> [(skill, requirement)]; build also has per-kind rows.
REGISTRY: Dict[str, List[Tuple[str, str]]] = {
    "build": [(M + "tdd", "required"), ("ponytail:ponytail", "optional")],
    "qa": [(M + "tdd", "required"), (M + "diagnosing-bugs", "required")],
    "review": [(M + "code-review", "required"), (M + "codebase-design", "required"),
               ("ponytail:ponytail-review", "optional")],
    "collect": [(M + "improve-codebase-architecture", "required")],
    "ui-refine-loop": [("impeccable:impeccable", "required")],
    "writing": [("humanizer:humanizer", "required")],
    "lint": [(M + "grilling", "optional")],
}
BUILD_KINDS: Dict[str, List[str]] = {
    "bug": [M + "diagnosing-bugs"],
    "feature": [M + "implement", M + "codebase-design"],
    "refactor": [M + "codebase-design"],
}
# What a lane does when an optional skill is missing.
FALLBACKS = {
    "ponytail:ponytail": "build the simplest thing that meets the criteria; no new abstraction "
                         "without a second caller",
    "ponytail:ponytail-review": "flag over-engineering in review: layers, options or abstractions "
                                "no criterion asks for",
    M + "grilling": "ask the user one question at a time until each criterion is checkable",
}
# How to install each pack in Claude Code. Checked against each repo's README
# and .claude-plugin/marketplace.json on 2026-10-27; ponytail is unverified.
INSTALL = {
    "mattpocock-skills": "/plugin install mattpocock-skills@claude-plugins-official\n"
                         "(or: /plugin marketplace add mattpocock/skills, then\n"
                         " /plugin install mattpocock-skills@mattpocock)",
    "impeccable": "/plugin marketplace add pbakaus/impeccable\n"
                  "/plugin install impeccable@impeccable",
    "humanizer": "/plugin marketplace add blader/humanizer\n"
                 "/plugin install humanizer@humanizer",
    "ponytail": "optional; install source not verified by factory",
}


def required_for(lane: str, kind: Optional[str] = None) -> List[Tuple[str, str]]:
    rows = list(REGISTRY[lane])
    if lane == "build":
        kinds = [kind] if kind else list(BUILD_KINDS)
        for k in kinds:
            rows += [(s, "required") for s in BUILD_KINDS.get(k, [])]
    seen, out = set(), []
    for name, req in rows:
        if name not in seen:
            seen.add(name)
            out.append((name, req))
    return out


# --- resolution --------------------------------------------------------------

def readable(path: str) -> Optional[str]:
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return None
        with open(real, encoding="utf-8") as fh:
            return real if fh.read().strip() else None
    except (OSError, UnicodeError):
        return None


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def frontmatter_name(skill_md: str) -> Optional[str]:
    try:
        with open(skill_md, encoding="utf-8") as fh:
            text = fh.read(4096)
    except (OSError, UnicodeError):
        return None
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return None
    n = re.search(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", m.group(1), re.M)
    return n.group(1).strip() if n else None


class Inventory:
    def __init__(self, home: str, project: str):
        self.home, self.project = home, project
        self.skill_dirs = [os.path.join(project, ".claude", "skills"), os.path.join(home, ".claude", "skills")]
        self.plugins: Dict[str, List[str]] = {}  # plugin name -> install paths
        self.disabled: set = set()
        self.searched: List[str] = list(self.skill_dirs)
        self._load_plugins()

    def _load_plugins(self) -> None:
        for path in (os.path.join(self.home, ".claude", "settings.json"),
                     os.path.join(self.project, ".claude", "settings.json"),
                     os.path.join(self.project, ".claude", "settings.local.json")):
            for key, on in (load_json(path).get("enabledPlugins") or {}).items():
                if on is False:
                    self.disabled.add(key)
                else:
                    self.disabled.discard(key)
        manifest = os.path.join(self.home, ".claude", "plugins", "installed_plugins.json")
        self.searched.append(manifest)
        for key, installs in (load_json(manifest).get("plugins") or {}).items():
            if key in self.disabled or not isinstance(installs, list):
                continue
            name = key.split("@", 1)[0]
            for item in installs:
                if not isinstance(item, dict) or not isinstance(item.get("installPath"), str):
                    continue
                project_path = item.get("projectPath")
                if item.get("scope") in ("project", "local") and project_path and \
                        os.path.realpath(project_path) != os.path.realpath(self.project):
                    continue
                self.plugins.setdefault(name, []).append(item["installPath"])

    def plugin_skills(self, install: str) -> Dict[str, str]:
        """skill name -> SKILL.md for one installed plugin."""
        found: Dict[str, str] = {}
        manifest = load_json(os.path.join(install, ".claude-plugin", "plugin.json"))
        extra = manifest.get("skills")
        roots = [os.path.join(install, "skills")]
        roots += [os.path.normpath(os.path.join(install, p)) for p in
                  ([extra] if isinstance(extra, str) else extra if isinstance(extra, list) else [])
                  if isinstance(p, str)]
        for root in roots:
            direct = os.path.join(root, "SKILL.md")
            if os.path.isfile(direct):  # the path is itself one skill
                found.setdefault(frontmatter_name(direct) or os.path.basename(root.rstrip(os.sep)), direct)
                continue
            if os.path.isdir(root):
                for child in sorted(os.listdir(root)):
                    md = os.path.join(root, child, "SKILL.md")
                    if os.path.isfile(md):
                        found.setdefault(child, md)
        return found

    def resolve(self, name: str) -> Optional[str]:
        pack, _, skill = name.rpartition(":")
        if pack:
            for install in self.plugins.get(pack, []):
                md = self.plugin_skills(install).get(skill)
                if md and readable(md):
                    return readable(md)
        for d in self.skill_dirs:
            hit = readable(os.path.join(d, skill, "SKILL.md"))
            if hit:
                return hit
        return None


# --- report ------------------------------------------------------------------

def check(inv: Inventory, wanted: Iterable[Tuple[str, str, str]]) -> List[dict]:
    rows = []
    for lane, name, req in wanted:
        path = inv.resolve(name)
        if path:
            status = "resolved"
        elif req == "optional":
            status = "fallback" if name in FALLBACKS else "skipped"
        else:
            status = "missing"
        rows.append({"lane": lane, "skill": name, "requirement": req, "status": status, "path": path,
                     "fallback": FALLBACKS.get(name) if status == "fallback" else None})
    return rows


def instructions(rows: List[dict]) -> str:
    packs = []
    for r in rows:
        if r["status"] == "missing":
            pack = r["skill"].split(":", 1)[0] if ":" in r["skill"] else r["skill"]
            if pack not in packs:
                packs.append(pack)
    out = []
    for pack in packs:
        names = ", ".join(sorted({r["skill"] for r in rows if r["status"] == "missing"
                                  and r["skill"].split(":", 1)[0] == pack}))
        out.append(f"missing {names}. Install in Claude Code, then restart the session:")
        out += ["  " + line for line in INSTALL.get(pack, f"install the {pack} skill").splitlines()]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Check that factory's required skills are installed.")
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--all", action="store_true")
    sel.add_argument("--lane", choices=sorted(REGISTRY))
    sel.add_argument("--skill", action="append")
    ap.add_argument("--kind", help="build card kind: " + ", ".join(BUILD_KINDS))
    ap.add_argument("--home", default=os.environ.get("FACTORY_SKILLS_HOME") or os.path.expanduser("~"))
    ap.add_argument("--project", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.kind and args.lane != "build":
        ap.error("--kind goes with --lane build")
    if args.kind and args.kind not in BUILD_KINDS:
        ap.error("--kind must be one of " + ", ".join(BUILD_KINDS))

    project = args.project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    inv = Inventory(os.path.abspath(args.home), os.path.abspath(project))
    if args.all:
        wanted = [(lane, n, r) for lane in REGISTRY for n, r in required_for(lane)]
    elif args.lane:
        wanted = [(args.lane, n, r) for n, r in required_for(args.lane, args.kind)]
    else:
        wanted = [("skill", n, "required") for n in args.skill]
    rows = check(inv, wanted)
    ready = not any(r["status"] == "missing" for r in rows)

    if args.json:
        print(json.dumps({"ready": ready, "skills": rows, "searched": inv.searched}, indent=2))
    else:
        marks = {"resolved": "✓", "missing": "✗", "fallback": "~", "skipped": "-"}
        for r in rows:
            note = r["path"] or ("fallback: " + r["fallback"] if r["fallback"] else r["requirement"])
            print(f"{marks[r['status']]} {r['lane']:<14} {r['skill']:<44} {note}")
        print("ready" if ready else "not ready")
    if not ready:
        print(instructions(rows), file=sys.stderr)
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
