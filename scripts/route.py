#!/usr/bin/env python3
"""route.py — which model works a card: the one place grade → model is decided.

A card is graded easy, medium or hard before a lane works it. The grade
comes from, in order:

  1. the card itself: a `difficulty:<grade>` label, else a
     `Difficulty: <grade>` line in the body;
  2. Jev's card_lint answer, when that point is live and answered;
  3. a cheap router agent (prompt in skills/board/references/router.md).

This script does the deterministic parts: reading a grade off a card,
reading the run flags, and mapping (host, tier, grade, lane) to a model.

Commands:
  route.py args [run args...]          JSON {host, tier, pin} for `run --low|--high --codex[=M]`
  route.py card <card-file|-> [--jev F] JSON {grade, source}; grade null = ask the router
  route.py model --lane L [--grade G] [--tier T] [--host H] [--pin M]
                                       print the lane model ("inherit" = session model)
  route.py router [--tier T] [--host H] [--pin M]
                                       print the router model ("none" = skip the router)
  route.py host                        print the host this shell runs in (claude|codex)

Model precedence for a lane, first match wins:
  --pin (from --codex=<model>) > codex.model (codex host) > models.<lane> (claude host)
  > routing.<host>.<tier>.<grade> > the built-in ladder below.
A missing or unknown grade takes the medium cell.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

GRADES = ("easy", "medium", "hard")
TIERS = ("low", "default", "high")
HOSTS = ("claude", "codex")
INHERIT = "inherit"  # use the orchestrating session's own model
NO_ROUTER = "none"

# host -> tier -> slot -> model. Slots are the three grades plus "router".
LADDERS: Dict[str, Dict[str, Dict[str, str]]] = {
    "claude": {
        "low":     {"router": "haiku",  "easy": "haiku",  "medium": "sonnet", "hard": "opus"},
        "default": {"router": "haiku",  "easy": "sonnet", "medium": "opus",   "hard": INHERIT},
        "high":    {"router": "sonnet", "easy": "opus",   "medium": "opus",   "hard": "opus"},
    },
    "codex": {
        "low":     {"router": "gpt-6-luna", "easy": "gpt-6-luna",  "medium": "gpt-6.1-sol", "hard": "gpt-6.1-sol"},
        "default": {"router": "gpt-6-luna", "easy": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "hard": "gpt-6-astra"},
        "high":    {"router": "gpt-6-luna", "easy": "gpt-6-astra", "medium": "gpt-6-astra", "hard": "gpt-6-astra"},
    },
}

ALIASES = {"low": "easy", "high": "hard", "med": "medium"}
LINE = re.compile(r"^[\s>*_-]*difficulty[\s*_]*:[\s*_]*([a-z]+)", re.IGNORECASE | re.MULTILINE)


# --- config ------------------------------------------------------------------

def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """The project config, or {} when there is none (the ladder still works)."""
    if not path:
        path = os.environ.get("FACTORY_CONFIG")
    if not path:
        try:
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                  text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            root = os.getcwd()
        path = os.path.join(root, ".claude", "factory.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _get(cfg: Dict[str, Any], *keys: str) -> Optional[str]:
    node: Any = cfg
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node.strip() if isinstance(node, str) and node.strip() else None


# --- grades --------------------------------------------------------------------

def norm_grade(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = ALIASES.get(v, v)
    return v if v in GRADES else None


def card_grade(card: Any) -> Optional[str]:
    """An explicit grade on the card: a difficulty:* label, else a Difficulty: line."""
    if isinstance(card, str):
        card = {"body": card}
    if not isinstance(card, dict):
        return None
    for label in card.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.lower().startswith("difficulty:"):
            g = norm_grade(name.split(":", 1)[1])
            if g:
                return g
    for match in LINE.finditer(str(card.get("body") or "")):
        g = norm_grade(match.group(1))
        if g:
            return g
    return None


def jev_grade(decision: Any) -> Optional[str]:
    """Jev's card_lint difficulty, only when it is live and answered."""
    if not isinstance(decision, dict) or decision.get("point", "card_lint") != "card_lint":
        return None
    if not (decision.get("available") and decision.get("act")):
        return None
    return norm_grade(decision.get("difficulty"))


def resolve(card: Any, jev: Any = None) -> Dict[str, Optional[str]]:
    g = card_grade(card)
    if g:
        return {"grade": g, "source": "card"}
    g = jev_grade(jev)
    if g:
        return {"grade": g, "source": "jev"}
    return {"grade": None, "source": None}


# --- models --------------------------------------------------------------------

def _check(host: str, tier: str) -> None:
    if host not in HOSTS:
        raise ValueError(f"unknown host: {host}")
    if tier not in TIERS:
        raise ValueError(f"unknown tier: {tier}")


def ladder_model(cfg: Dict[str, Any], host: str, tier: str, slot: str) -> str:
    _check(host, tier)
    return _get(cfg, "routing", host, tier, slot) or LADDERS[host][tier][slot]


def pinned(cfg: Dict[str, Any], host: str, pin: Optional[str]) -> Optional[str]:
    if pin and pin.strip():
        return pin.strip()
    return _get(cfg, "codex", "model") if host == "codex" else None


def lane_model(cfg: Dict[str, Any], lane: str, grade: Optional[str] = None, tier: str = "default",
               host: str = "claude", pin: Optional[str] = None) -> str:
    _check(host, tier)
    fixed = pinned(cfg, host, pin)
    if fixed:
        return fixed
    if host == "claude":
        override = _get(cfg, "models", lane)
        if override:
            return override
    return ladder_model(cfg, host, tier, norm_grade(grade) or "medium")


def router_model(cfg: Dict[str, Any], tier: str = "default", host: str = "claude",
                 pin: Optional[str] = None) -> str:
    _check(host, tier)
    if pinned(cfg, host, pin):
        return NO_ROUTER  # one model for every card; nothing to grade
    return ladder_model(cfg, host, tier, "router")


# --- host and run flags -------------------------------------------------------

def detect_host(env: Optional[Dict[str, str]] = None) -> str:
    """FACTORY_HOST wins; Codex sets CODEX_THREAD_ID/CODEX_SESSION_ID in its shells."""
    env = os.environ if env is None else env
    forced = (env.get("FACTORY_HOST") or "").strip().lower()
    if forced in HOSTS:
        return forced
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"):
        return "codex"
    return "claude"


def parse_run_args(args: List[str], env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    host, tier, pin = detect_host(env), "default", None
    for a in args:
        if a == "--low":
            tier = "low"
        elif a == "--high":
            tier = "high"
        elif a == "--codex":
            host = "codex"
        elif a.startswith("--codex="):
            host, pin = "codex", a.split("=", 1)[1].strip()
            if not pin:
                raise ValueError("--codex=<model> needs a model name")
    if "--low" in args and "--high" in args:
        raise ValueError("--low and --high are mutually exclusive")
    return {"host": host, "tier": tier, "pin": pin}


# --- CLI -------------------------------------------------------------------------

def _read(path: str) -> Any:
    raw = sys.stdin.read() if path == "-" else open(path).read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "args":  # free-form: the run command's own arguments
        try:
            print(json.dumps(parse_run_args([a for a in argv[1:] if a != "--"])))
        except ValueError as e:
            print(f"route: {e}", file=sys.stderr)
            return 2
        return 0

    p = argparse.ArgumentParser(prog="route.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", help="config file (default: $FACTORY_CONFIG or .claude/factory.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card")
    c.add_argument("card", help="gh issue view --json body,labels output, plain body text, or -")
    c.add_argument("--jev", help="file with `jev.py decide card_lint` output")
    for name in ("model", "router"):
        s = sub.add_parser(name)
        if name == "model":
            s.add_argument("--lane", required=True, choices=["build", "qa", "review"])
            s.add_argument("--grade")
        s.add_argument("--tier", default="default", choices=TIERS)
        s.add_argument("--host", default=None, choices=HOSTS)
        s.add_argument("--pin")
    sub.add_parser("host")
    a = p.parse_args(argv)

    if a.cmd == "host":
        print(detect_host())
        return 0
    if a.cmd == "card":
        jev = _read(a.jev) if a.jev else None
        print(json.dumps(resolve(_read(a.card), jev)))
        return 0
    cfg = load_config(a.config)
    host = a.host or detect_host()
    if a.cmd == "model":
        if a.grade and not norm_grade(a.grade):
            print(f"route: unknown grade {a.grade!r} (easy|medium|hard)", file=sys.stderr)
            return 2
        print(lane_model(cfg, a.lane, a.grade, a.tier, host, a.pin))
    else:
        print(router_model(cfg, a.tier, host, a.pin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
