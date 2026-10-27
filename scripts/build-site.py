#!/usr/bin/env python3
"""build-site.py — assemble the GitHub Pages site into _site/ from the repo itself.

Usage (from the repo root):

  build-site.py [--root DIR] [--out _site] [--source-base URL]

Copies site/ (except its template), fills site/index.html's {{slots}} from
README.md (tagline, intro, flow diagram, lane bullets, Install, Use) and
from the skills' frontmatter + skills/families.json, and renders the skill
map to skill-map/index.html with scripts/skill-map.py. Nothing on the site is
typed twice, so it can't drift from the repo: a README section the page
needs that has gone missing fails the build instead of shipping a gap.

--source-base defaults to https://github.com/<repo>/blob/<ref>/, from
plugin.json's repository and $GITHUB_SHA (else "main").
Python 3.9+, standard library only.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


visual = _load("factory_visual", "visual.py")
skillmap = _load("factory_skill_map", "skill-map.py")


class BuildError(Exception):
    pass


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def readme_sections(text: str) -> dict:
    """{'#': preamble under the H1, 'Install': body, ...} keyed by H2 title."""
    out, key, buf, fence = {}, None, [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
        if not fence and re.match(r"#{1,2}\s", line):
            if key is not None:
                out[key] = "\n".join(buf).strip()
            key = "#" if line.startswith("# ") else line[3:].strip()
            buf = []
            continue
        buf.append(line)
    if key is not None:
        out[key] = "\n".join(buf).strip()
    return out


def need(sections: dict, *names) -> str:
    for n in names:
        for k, v in sections.items():
            if k.lower().startswith(n.lower()):
                return v
    raise BuildError("README.md has no '%s' section; the site needs it" % names[0])


def split_blocks(body: str):
    """-> (text before the first code fence, [code blocks], text after the first fence)."""
    m = re.search(r"```[^\n]*\n(.*?)\n```", body, re.S)
    if not m:
        return body, [], ""
    blocks = re.findall(r"```[^\n]*\n(.*?)\n```", body, re.S)
    return body[: m.start()].strip(), blocks, body[m.end():].strip()


def git_ref(root: str) -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:12]
    p = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() or "main"


def plugin_info(root: str) -> dict:
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        info = {}
    repo_url = str(info.get("repository") or info.get("homepage") or "")
    m = re.match(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo_url)
    return {"version": str(info.get("version") or "0"), "description": str(info.get("description") or ""),
            "repo": m.group(1) if m else "", "repo_url": repo_url if m else ""}


def skill_cards(skills: list, group: str, title: str) -> str:
    cards = []
    for s in skills:
        if s["group"] != group:
            continue
        lane = group == skillmap.BOARD_RUNS
        cmd = s["name"] if lane else "/factory:%s" % s["name"]
        hint = '<span class="hint">%s</span>' % esc(s["hint"]) if s["hint"] else ""
        cards.append('<div class="card%s"><code class="cmd">%s</code><p>%s</p>%s</div>' % (
            " lane" if lane else "", esc(cmd), esc(s["blurb"]), hint))
    if not cards:
        return ""
    return "<h3>%s</h3><div class=\"cards\">%s</div>" % (esc(title), "".join(cards))


def build(root: str, out: str, source_base: str | None = None) -> dict:
    root, out = os.path.abspath(root), os.path.abspath(out)
    site = os.path.join(root, "site")
    tpl_path = os.path.join(site, "index.html")
    if not os.path.isfile(tpl_path):
        raise BuildError("site/index.html is missing")
    with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
        sections = readme_sections(fh.read())
    info = plugin_info(root)
    ref = git_ref(root)
    if source_base is None and info["repo"]:
        source_base = "https://github.com/%s/blob/%s/" % (info["repo"], ref)

    pre = need(sections, "#")
    tag = re.search(r"\*\*(.+?)\*\*", pre)
    if not tag:
        raise BuildError("README.md needs a **bold tagline** under the title")
    intro, blocks, after = split_blocks(pre[tag.end():].strip())
    if not blocks:
        raise BuildError("README.md needs the board flow diagram (a code block) under the title")
    install = need(sections, "Install")
    use_text, use_blocks, use_after = split_blocks(need(sections, "Use"))
    if not use_blocks:
        raise BuildError("README.md 'Use' section needs a code block of commands")

    fam = skillmap.load_families(root)
    skills = skillmap.read_skills(root, fam)
    titles = {g.get("id"): g.get("title") for g in fam.get("groups", [])}
    spec = skillmap.build_spec(root)

    slots = {
        "tagline": esc(tag.group(1)),
        "description": esc(info["description"] or tag.group(1)),
        "intro": visual.md(re.sub(r":\s*$", ".", intro)),
        "flow": esc(blocks[0]),
        "lanes": visual.md(after),
        "install": visual.md(install),
        "use": esc(use_blocks[0]),
        "you_type": skill_cards(skills, skillmap.YOU_TYPE, titles.get(skillmap.YOU_TYPE) or "You type"),
        "board_runs": skill_cards(skills, skillmap.BOARD_RUNS, titles.get(skillmap.BOARD_RUNS) or "The board runs"),
        "counts": "".join("<span>%s %s</span>" % (esc(v), esc(k)) for k, v in spec["meta"].items()),
        "version": esc(info["version"]),
        "repo": esc(info["repo"]),
        "repo_url": esc(info["repo_url"] or "#"),
        "commit": esc(ref),
    }
    with open(tpl_path, encoding="utf-8") as fh:
        page = fh.read()
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", page)) - set(slots))
    if missing:
        raise BuildError("site/index.html uses unknown slots: %s" % ", ".join(missing))
    page = re.sub(r"\{\{(\w+)\}\}", lambda m: slots[m.group(1)], page)

    if os.path.isdir(out):
        shutil.rmtree(out)
    shutil.copytree(site, out, ignore=shutil.ignore_patterns("index.html", ".*", "__pycache__"))
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    spec["generated"] = "built from %s" % ref
    visual.write_page(os.path.join(out, "skill-map", "index.html"), visual.render_page(spec, source_base))
    open(os.path.join(out, ".nojekyll"), "w").close()
    files = sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs)
    return {"out": out, "files": files, "skills": len(skills)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="build-site.py", description="Build the factory docs site into _site/.")
    p.add_argument("--root", default=os.path.dirname(HERE))
    p.add_argument("--out")
    p.add_argument("--source-base")
    a = p.parse_args(argv)
    try:
        res = build(a.root, a.out or os.path.join(a.root, "_site"), a.source_base)
    except BuildError as e:
        print("build-site: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
