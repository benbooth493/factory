#!/usr/bin/env python3
"""lint-cards.py — flag cards whose acceptance criteria a lane can't check.

A Build lane can't ask questions. A card that says "should work better" gets
built on a guess. This finds those cards before they are dispatched, with the
same answer every time (no model involved): rules, not judgement.

Usage:
  lint-cards.py                    lint Ready cards (via board.sh + gh issue view)
  lint-cards.py --backlog          Ready and Backlog
  lint-cards.py --issue 12 --issue 15
  lint-cards.py --cards file.json  lint a JSON array of {number,title,body,labels}
  add --json for machine output (the board skill reads this)

Problems, one code each:
  no-criteria      no checklist and no "Acceptance criteria" section
  empty-criteria   the section is there but has nothing in it
  vague            a criterion leans on a weasel word ("should work", "better", "etc")
  not-observable   a criterion names no outcome anyone could see or measure
  no-repro         a bug with no steps to reproduce
  multiple-asks    the title asks for several unrelated things
  too-big          more criteria than lint.max_criteria (default 8)

Exit codes: 0 every card is clear, 1 at least one card has a problem,
2 bad usage, config or GitHub failure.

Config (.claude/factory.json, optional):
  "lint": {"max_criteria": 8, "extra_vague_words": ["snappy"], "columns": ["ready"]}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# Weasel words that never say when a card is done.
VAGUE = [
    r"should work", r"works?(?: fine| well| correctly| properly)?\s*$", r"properly", r"correctly",
    r"etc\.?", r"and so on", r"as expected", r"as needed", r"user[- ]friendly", r"intuitive",
    r"seamless(?:ly)?", r"tbd", r"todo:", r"if possible", r"somehow", r"and more",
]
# Comparatives are fine with a number next to them ("under 200 ms faster").
UNMEASURED = [
    r"better", r"improve[sd]?", r"improvements?", r"faster", r"quick(?:er|ly)?", r"snappier",
    r"nicer?", r"robust", r"polish(?:ed)?", r"modern", r"clean(?:er)? up", r"cleaner", r"smoother",
    r"more (?:reliable|stable|responsive)", r"optimi[sz]ed?",
]
NUMBER = re.compile(r"\d")

# Something a tester could point at: a number, a literal, a path or URL, a
# Given/When/Then, or a verb that names a visible result.
OBSERVABLE = re.compile(
    r"\d|`[^`]+`|\"[^\"]+\"|'[^']+'|https?://|(?:^|\s)/\w|\bgiven\b|\bwhen\b.*\bthen\b|"
    r"\b(?:returns?|shows?|displays?|renders?|responds?|redirects?|logs?|emits?|fails?|errors?|"
    r"contains?|equals?|exits?|creates?|deletes?|removes?|sends?|appears?|disappears?|visible|"
    r"hidden|lists?|prints?|writes?|reads?|matches|rejects?|accepts?|saves?|stores?|loads?|"
    r"opens?|closes?|disabled|enabled|navigates?|highlights?|sorts?|filters?|counts?|"
    r"passes|blocks?|allows?|denies|raises?|throws?|status|message|toast|banner|"
    r"downloads?|uploads?|links?|persist(?:s|ed)?|retries|times? out|switch(?:es)?|turns?|changes?|"
    r"increments?|decrements?|includes?|excludes?|skips?|marks?|sets?|becomes?)\b",
    re.I,
)

ASK_VERBS = ("add", "fix", "remove", "update", "make", "implement", "support", "refactor", "change",
             "create", "migrate", "rename", "improve", "allow", "show", "build", "replace", "delete",
             "move", "redesign", "rewrite", "upgrade", "introduce", "enable", "disable")

CRITERIA_HEADING = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)\s*(?:acceptance criteria|definition of done|done when)\b", re.I)
ANY_HEADING = re.compile(r"^\s*(?:#{1,6}\s+\S|\*\*[^*]+\*\*\s*$)")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.*\S)\s*$")
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s*(.*\S)\s*$")
REPRO = re.compile(r"steps to reproduce|\brepro(?:duce|duction)?\b|to reproduce|\bsteps:", re.I)


def die(msg: str) -> None:
    print(f"lint-cards: {msg}", file=sys.stderr)
    sys.exit(2)


def strip_fences(body: str) -> List[str]:
    lines, fenced = [], False
    for line in (body or "").splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def criteria(body: str) -> Optional[List[str]]:
    """The card's criteria; [] for an empty section; None when there is none at all."""
    lines = strip_fences(body)
    for i, line in enumerate(lines):
        if CRITERIA_HEADING.match(line):
            items = []
            for after in lines[i + 1:]:
                if ANY_HEADING.match(after) and not ITEM.match(after):
                    break
                m = ITEM.match(after)
                if m:
                    items.append(m.group(1))
            return items
    boxes = [m.group(1) for m in map(CHECKBOX.match, lines) if m]
    return boxes or None


def labels_of(card: Dict[str, Any]) -> List[str]:
    return [(l.get("name") if isinstance(l, dict) else str(l)).lower() for l in card.get("labels") or []]


def is_bug(card: Dict[str, Any]) -> bool:
    labels = labels_of(card)
    if "bug" in labels or "kind:bug" in labels:
        return True
    return bool(re.match(r"^\s*(?:\[?bug\]?:?|fix(?:es|ed)?\b)", card.get("title") or "", re.I))


def asks(title: str) -> List[str]:
    parts = re.split(r"\s+(?:and|&|\+|plus|also)\s+|;\s*|,\s*(?:and\s+)?", title or "", flags=re.I)
    return [p.strip() for p in parts if p.strip().split(" ")[0].lower().rstrip(":") in ASK_VERBS]


def lint_card(card: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    cfg = cfg or {}
    words = r"\b(?:" + "|".join(VAGUE + [re.escape(w) for w in cfg.get("extra_vague_words", [])]) + r")"
    vague = re.compile(words, re.I)
    unmeasured = re.compile(r"\b(?:" + "|".join(UNMEASURED) + r")\b", re.I)
    problems: List[Dict[str, str]] = []

    def add(code: str, detail: str) -> None:
        problems.append({"code": code, "detail": detail})

    body = card.get("body") or ""
    items = criteria(body)
    if items is None:
        add("no-criteria", "no checklist and no 'Acceptance criteria' section")
    elif not items:
        add("empty-criteria", "the acceptance criteria section is empty")
    for item in items or []:
        hit = vague.search(item) or (None if NUMBER.search(item) else unmeasured.search(item))
        if hit:
            add("vague", f"'{hit.group(0).strip()}' in: {item}")
        elif not OBSERVABLE.search(item):
            add("not-observable", f"no visible or measurable outcome in: {item}")
    if is_bug(card) and not REPRO.search(body):
        add("no-repro", "bug with no steps to reproduce")
    many = asks(card.get("title") or "")
    if len(many) > 1:
        add("multiple-asks", "title asks for " + " / ".join(many) + "; split into one card each")
    limit = int(cfg.get("max_criteria", 8))
    if items and len(items) > limit:
        add("too-big", f"{len(items)} criteria (lint.max_criteria is {limit}); split the card")
    return problems


# --- reading the board -------------------------------------------------------

def repo_root() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.getcwd()


def load_config() -> Dict[str, Any]:
    path = os.environ.get("FACTORY_CONFIG") or os.path.join(repo_root(), ".claude", "factory.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        die(f"cannot read config {path}: {exc}")
    return {}


def run_json(cmd: List[str]) -> Any:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out or "null")
    except subprocess.CalledProcessError as exc:
        die(f"{' '.join(cmd[:3])} failed: {exc.stderr.strip()}")
    except (OSError, ValueError) as exc:
        die(f"{' '.join(cmd[:3])}: {exc}")


def board_cards(columns: Iterable[str], repo: str) -> List[Dict[str, Any]]:
    cards = []
    for col in columns:
        for item in run_json(["bash", os.path.join(HERE, "board.sh"), "items", col]) or []:
            cards.append(dict(issue(item["number"], repo), column=item.get("status") or col))
    return cards


def issue(number: int, repo: str) -> Dict[str, Any]:
    data = run_json(["gh", "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,labels"])
    if not isinstance(data, dict):
        die(f"gh returned no issue for #{number}")
    return data


def render(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "no cards to lint"
    out = []
    for r in results:
        mark = "ok  " if not r["problems"] else "FLAG"
        out.append(f"{mark} #{r['number']:<5} {(r.get('title') or '')[:60]}")
        for p in r["problems"]:
            out.append(f"       {p['code']:<15} {p['detail']}")
    flagged = sum(1 for r in results if r["problems"])
    out.append(f"{flagged} of {len(results)} card(s) need tightening")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Flag cards with vague acceptance criteria.")
    ap.add_argument("--backlog", action="store_true", help="also lint Backlog")
    ap.add_argument("--issue", type=int, action="append", default=[], help="lint just these issues")
    ap.add_argument("--cards", help="JSON array of cards instead of reading GitHub")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.cards:
        try:
            with open(args.cards, encoding="utf-8") as fh:
                cards = json.load(fh)
        except (OSError, ValueError) as exc:
            die(f"cannot read {args.cards}: {exc}")
        cfg = {}
        if os.environ.get("FACTORY_CONFIG"):
            cfg = load_config()
    else:
        cfg = load_config()
        repo = cfg.get("repo") or die("config has no repo")
        if args.issue:
            cards = [issue(n, repo) for n in args.issue]
        else:
            columns = list((cfg.get("lint") or {}).get("columns") or ["ready"])
            if args.backlog and "backlog" not in columns:
                columns.append("backlog")
            cards = board_cards(columns, repo)
    if not isinstance(cards, list):
        die("cards must be a JSON array")

    lint_cfg = cfg.get("lint") or {}
    results = [{"number": c.get("number"), "title": c.get("title"), "column": c.get("column"),
                "problems": lint_card(c, lint_cfg)} for c in cards]
    print(json.dumps(results, indent=2) if args.json else render(results))
    return 1 if any(r["problems"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
