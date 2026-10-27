#!/usr/bin/env python3
"""Lint text against factory's writing standard (docs/writing-standard.md).

Usage:
  check-writing.py commit  <file|->
  check-writing.py pr      <file|-> [--title "PR title"]
  check-writing.py card    <file|-> [--title "Card title"]
  check-writing.py comment <file|->

Prints one `file:line: problem` per failure and exits 1; exits 0 when clean,
2 on bad usage. Only the mechanical rules are checked; plain-words judgement
is for the writer (and the humanizer skill). Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import List, Optional, Tuple

SUBJECT_MAX = 72
CARD_TITLE_MAX = 80
WRAP = 72

BANNED = [
    "this pr aims to", "this commit aims to", "this change aims to", "aims to",
    "in order to", "successfully", "seamless", "seamlessly", "robust",
    "comprehensive", "leverage", "utilize", "delve", "cutting-edge",
    "game-changer", "world-class", "best-in-class", "state-of-the-art",
    "it's worth noting", "it is worth noting", "please note that", "as an ai",
]

# First words that show a subject is not imperative. "-ed"/"-ing" are caught
# by suffix; third-person "-s" only for common verbs, so "Docs: ..." is fine.
NOT_VERBS_ED = {"need", "embed", "seed", "feed", "speed", "shed", "bed", "red", "shred", "breed", "proceed", "exceed", "succeed"}
NOT_VERBS_ING = {"bring", "string", "ring", "sing", "spring", "wing", "swing", "sting", "king", "thing", "ping"}
THIRD_PERSON = {
    "adds", "fixes", "updates", "removes", "changes", "makes", "uses", "moves",
    "renames", "implements", "introduces", "improves", "refactors", "creates",
    "deletes", "handles", "allows", "ensures", "supports", "bumps", "cleans",
    "converts", "enables", "disables", "extracts", "replaces", "reverts",
    "simplifies", "splits", "merges", "drops", "tweaks", "corrects", "prevents",
}
PREFIX = re.compile(r"^[a-z]+(\([^)]*\))?!?:\s*")   # conventional-commit type(scope):
CLOSES = re.compile(r"\b(closes|fixes|resolves|refs)\s+([\w.-]+/[\w.-]+)?#\d+", re.I)
CHECKBOX = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+\S")
TRAILER = re.compile(r"^[A-Za-z][A-Za-z-]*:\s")

PR_SECTIONS = [("Why",), ("What changed",), ("Acceptance criteria",)]
CARD_SECTIONS = [("Problem", "What is happening"), ("Acceptance criteria",)]

Problem = Tuple[int, str]   # (line number, 0 = title / whole text; message)


def strip_code(lines: List[str]) -> List[str]:
    """Blank out fenced blocks and inline code so prose rules skip them."""
    out, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`[^`]*`", "", line))
    return out


def banned_phrases(lines: List[str]) -> List[Problem]:
    found = []
    for n, line in enumerate(strip_code(lines), 1):
        low, taken = line.lower(), []
        for phrase in BANNED:   # longer phrases come first in BANNED
            m = re.search(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", low)
            if m and not any(s <= m.start() and m.end() <= e for s, e in taken):
                taken.append(m.span())   # "this pr aims to" hides "aims to"
                found.append((n, f'banned phrase "{phrase}"'))
    return found


def check_subject(subject: str, line: int, limit: int = SUBJECT_MAX) -> List[Problem]:
    problems: List[Problem] = []
    if not subject.strip():
        return [(line, "subject is empty")]
    if subject != subject.strip():
        problems.append((line, "subject has leading or trailing whitespace"))
    if len(subject) > limit:
        problems.append((line, f"subject is {len(subject)} chars; keep it to {limit}"))
    if subject.rstrip().endswith("."):
        problems.append((line, "subject ends with a period"))
    words = PREFIX.sub("", subject.strip()).split()
    first = words[0].lower().strip(",:;") if words else ""
    if (first.endswith("ed") and first not in NOT_VERBS_ED and len(first) > 3) \
            or (first.endswith("ing") and first not in NOT_VERBS_ING and len(first) > 4) \
            or first in THIRD_PERSON:
        problems.append((line, f'subject starts with "{words[0]}"; use the imperative ("Add", "Fix", "Remove")'))
    return problems


def check_commit(lines: List[str]) -> List[Problem]:
    # Git drops '#' comment lines and trailing blank lines; do the same.
    numbered = [(n, l) for n, l in enumerate(lines, 1) if not l.startswith("#")]
    while numbered and not numbered[-1][1].strip():
        numbered.pop()
    while numbered and not numbered[0][1].strip():
        numbered.pop(0)
    if not numbered:
        return [(1, "commit message is empty")]
    n0, subject = numbered[0]
    problems = check_subject(subject, n0)
    if len(numbered) > 1 and numbered[1][1].strip():
        problems.append((numbered[1][0], "leave a blank line after the subject"))
    fenced = False
    for n, line in numbered[1:]:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if fenced or line.startswith("    ") or TRAILER.match(line):
            continue
        if len(line) > WRAP and " " in line.strip() and "://" not in line:
            problems.append((n, f"body line is {len(line)} chars; wrap at {WRAP}"))
    problems += banned_phrases(["" if l.startswith("#") else l for l in lines])
    return problems


def headings(lines: List[str]) -> List[Tuple[int, str]]:
    return [(n, re.sub(r"^#{2,3}\s+", "", l).strip().rstrip(":").lower())
            for n, l in enumerate(strip_code(lines), 1) if re.match(r"^#{2,3}\s+\S", l)]


def section_body(lines: List[str], start: int) -> List[str]:
    body = []
    for line in lines[start:]:
        if re.match(r"^#{1,3}\s+\S", line):
            break
        body.append(line)
    return body


def check_sections(lines: List[str], required) -> List[Problem]:
    problems: List[Problem] = []
    found = headings(lines)
    names = [h for _, h in found]
    for options in required:
        if not any(o.lower() in names for o in options):
            problems.append((0, f'missing section "## {options[0]}"'))
    for n, name in found:
        if name == "acceptance criteria":
            items = [l for l in section_body(lines, n) if CHECKBOX.match(l)]
            if not items:
                problems.append((n, 'acceptance criteria need at least one "- [ ]" item'))
    return problems


def check_title(title: Optional[str], limit: int, card: bool) -> List[Problem]:
    if title is None:
        return []
    if card:
        problems = []
        if not title.strip():
            return [(0, "title is empty")]
        if len(title) > limit:
            problems.append((0, f"title is {len(title)} chars; keep it to {limit}"))
        if title.rstrip().endswith("."):
            problems.append((0, "title ends with a period"))
        if re.match(r"^\s*#?\d+\b", title):
            problems.append((0, "title starts with an issue number; drop it"))
        return problems
    return check_subject(title, 0, limit)


def check_pr(lines: List[str], title: Optional[str]) -> List[Problem]:
    problems = check_title(title, SUBJECT_MAX, card=False)
    if not any(CLOSES.search(l) for l in strip_code(lines)):
        problems.append((0, 'missing "Closes #N" (or "Refs #N" for partial work)'))
    problems += check_sections(lines, PR_SECTIONS)
    problems += banned_phrases(lines)
    return problems


def check_card(lines: List[str], title: Optional[str]) -> List[Problem]:
    problems = check_title(title, CARD_TITLE_MAX, card=True)
    problems += check_sections(lines, CARD_SECTIONS)
    problems += banned_phrases(lines)
    return problems


def check_comment(lines: List[str]) -> List[Problem]:
    if not any(l.strip() for l in lines):
        return [(1, "comment is empty")]
    return banned_phrases(lines)


def check(kind: str, text: str, title: Optional[str] = None) -> List[Problem]:
    lines = text.splitlines()
    if kind == "commit":
        return check_commit(lines)
    if kind == "pr":
        return check_pr(lines, title)
    if kind == "card":
        return check_card(lines, title)
    if kind == "comment":
        return check_comment(lines)
    raise ValueError(f"unknown kind: {kind}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Lint text against the factory writing standard.")
    ap.add_argument("kind", choices=["commit", "pr", "card", "comment"])
    ap.add_argument("file", help="path, or - for stdin")
    ap.add_argument("--title", help="PR or card title to check too")
    args = ap.parse_args(argv)
    if args.title is not None and args.kind not in ("pr", "card"):
        ap.error("--title only applies to pr and card")
    if args.file == "-":
        name, text = "<stdin>", sys.stdin.read()
    else:
        name = args.file
        try:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"check-writing: {exc}", file=sys.stderr)
            return 2
    problems = sorted(check(args.kind, text, args.title), key=lambda p: p[0])
    for line, msg in problems:
        where = "title" if line == 0 and msg.startswith(("title", "subject")) else (
            f"{name}:{line}" if line else name)
        print(f"{where}: {msg}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
