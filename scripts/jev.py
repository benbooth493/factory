#!/usr/bin/env python3
"""jev.py — fast, cheap typed decisions for factory, via TypeSafe's Jev on Vercel AI Gateway.

Jev answers questions you define (yes/no, ordered score, choice) about a
piece of state, with a probability for each answer. factory uses it at four
decision points, each of which is `off`, `shadow` or `live` in config:

  collect_triage  Sentry/PostHog candidate: actionable, noise, or expected?
                  live: drop near-certain noise before a Claude verifier runs.
  card_lint       Ready card: checkable acceptance criteria? how hard?
                  live: its difficulty grades the card for the router
                  (scripts/route.py maps the grade to a model).
  risk_veto       PR about to merge: does it touch sensitive areas?
                  live: hold the merge for a human. It can only ever block.
  bounce_reason   QA/Review failure: what kind of failure is it?
                  live: label the bounce so Build starts in the right place.

In shadow mode Jev's decision is logged but nothing acts on it. The skills
record what Claude decided at the same point, and `report` shows how often
the two agree, so you can switch a point to live on evidence.

Commands:
  jev.py check                                 read-only connectivity test
  jev.py decide <point> <state-file|-> --id ID JSON decision (never fails the caller)
  jev.py gate <pr>                             risk_veto for a PR; exit 4 = hold (live only)
  jev.py record <point> <id> <claude-label>    log Claude's decision for comparison
  jev.py report                                agreement and spend per point

Key: AI_GATEWAY_API_KEY from the environment or the repo's .env.
Log: .claude/factory/jev-log.jsonl (keep it out of git).
Any failure (no key, network, budget reached) returns available=false and
factory carries on exactly as it would without Jev.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import load_config, repo_root, secret  # noqa: E402

DEFAULT_BASE = "https://ai-gateway.vercel.sh/typesafe"
DEFAULT_MODEL = "typesafe-ai/jev"
MAX_STATE_CHARS = 90_000  # Jev takes 32k tokens of state; stay well under
EXIT_HOLD = 4
POINTS = ("collect_triage", "card_lint", "risk_veto", "bounce_reason")

DEFAULT_THRESHOLDS = {
    "noise": 0.9,          # collect_triage: P(noise) needed to drop a candidate
    "criteria": 0.5,       # card_lint: P(has checkable criteria) to pass
    "sensitive": 0.5,      # risk_veto: P(sensitive) that holds a merge
}

# --- the questions ------------------------------------------------------------
# Answer sets are part of the contract: changing them invalidates shadow data.

QUESTIONS: Dict[str, Dict[str, dict]] = {
    "collect_triage": {
        "signal": {
            "type": "choice",
            "instructions": "This is a production error or user-behaviour signal from a web app. "
                            "What is it most likely to be?",
            "criteria": {
                "actionable": "A defect in the app's own code or UX that engineers should fix",
                "noise": "Caused by bots, crawlers, browser extensions, ad blockers, third-party "
                         "scripts, network blips or a test environment, not the app's code",
                "expected": "Deliberate behaviour working as designed, such as validation errors, "
                            "rate limits, auth redirects or cancelled requests",
            },
        },
        "severity": {
            "type": "score",
            "instructions": "How badly does this affect users of the app?",
            "criteria": [
                "Cosmetic or invisible to users",
                "Annoying, but users can carry on",
                "Blocks a feature for some users",
                "Breaks a core flow such as sign-up, checkout or data loss",
            ],
        },
    },
    "card_lint": {
        "criteria": {
            "type": "noul",
            "instructions": "Does this ticket contain acceptance criteria that a developer could "
                            "check objectively, by running code or a test?",
        },
        "difficulty": {
            "type": "score",
            "instructions": "How much engineering work does this ticket need?",
            "criteria": [
                "A small, local change in one or two files with an obvious approach",
                "A moderate change across several files or one unfamiliar area",
                "A large or risky change: cross-cutting design, data migration, concurrency, "
                "security or an unclear approach",
            ],
        },
    },
    "risk_veto": {
        "sensitive": {
            "type": "noul",
            "instructions": "Does this code change touch authentication, authorisation, payments, "
                            "secrets handling, database schema or data migrations, deletion of "
                            "user data, or cloud infrastructure permissions and networking?",
        },
    },
    "bounce_reason": {
        "reason": {
            "type": "choice",
            "instructions": "A reviewer sent this code change back. What is the main reason?",
            "criteria": {
                "missing_test": "Behaviour is not covered, or tests do not assert the requirement",
                "logic_bug": "The code does the wrong thing or fails in a case it should handle",
                "criteria_misread": "The change solves a different problem than the ticket asked",
                "scope_creep": "The change includes work the ticket did not ask for",
                "environment": "Flaky tests, CI or environment problems unrelated to the code",
            },
        },
    },
}


# --- config, log, budget -----------------------------------------------------

def settings(cfg: dict) -> dict:
    s = dict(cfg.get("jev") or {})
    s.setdefault("enabled", False)
    s.setdefault("base_url", DEFAULT_BASE)
    s.setdefault("model", DEFAULT_MODEL)
    s.setdefault("monthly_budget_usd", 5.0)
    s["points"] = {p: (s.get("points") or {}).get(p, "shadow") for p in POINTS}
    s["thresholds"] = {**DEFAULT_THRESHOLDS, **(s.get("thresholds") or {})}
    return s


def log_path() -> str:
    return os.environ.get("FACTORY_JEV_LOG") or os.path.join(repo_root(), ".claude", "factory", "jev-log.jsonl")


def append_log(entry: dict) -> None:
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **entry}
    with open(path, "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def read_log() -> List[dict]:
    try:
        with open(log_path()) as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


def spent_this_month(entries: List[dict]) -> float:
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    total = 0.0
    for e in entries:
        if e.get("kind") == "jev" and str(e.get("ts", "")).startswith(month):
            try:
                total += float(e.get("cost") or 0)
            except (TypeError, ValueError):
                pass
    return total


# --- transport -----------------------------------------------------------------

class JevError(Exception):
    pass


def http_post(url: str, key: str, body: dict, timeout: int = 20) -> dict:
    fake = os.environ.get("FACTORY_JEV_FAKE_RESPONSE")
    if fake:  # test hook: replay a canned gateway response
        with open(fake) as fh:
            return json.load(fh)
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "factory-jev/0.3 (+https://github.com/benbooth493/factory)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("message", "")
        except ValueError:
            detail = ""
        raise JevError(f"HTTP {e.code}: {detail}"[:300])
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise JevError(f"cannot reach AI Gateway: {e}")


HTTP: Callable[..., dict] = http_post


def ask(s: dict, state: Any, questions: Dict[str, dict]) -> dict:
    key = secret("AI_GATEWAY_API_KEY")
    if not key:
        raise JevError("AI_GATEWAY_API_KEY is not set (environment or .env)")
    if isinstance(state, str) and len(state) > MAX_STATE_CHARS:
        state = state[:MAX_STATE_CHARS] + "\n[truncated]"
    body = {"model": s["model"], "state": state, "questions": questions}
    resp = HTTP(s["base_url"].rstrip("/") + "/v1/systemone", key, body)
    if not isinstance(resp, dict) or "answers" not in resp:
        raise JevError(f"unexpected response: {str(resp)[:200]}")
    return resp


def cost_of(resp: dict) -> float:
    gw = ((resp.get("provider_metadata") or {}).get("gateway") or {})
    try:
        return float(gw.get("cost") or 0)
    except (TypeError, ValueError):
        return 0.0


# --- turning answers into factory decisions ---------------------------------

def prob(answer: dict, option: str) -> float:
    return float((answer.get("probabilities") or {}).get(option, 0.0))


def tier(score: float, top: int) -> str:
    """Map a 0..top probability-weighted score onto easy/medium/hard."""
    frac = score / top if top else 0
    return "easy" if frac < 1 / 3 else ("medium" if frac < 2 / 3 else "hard")


def interpret(point: str, answers: Dict[str, dict], s: dict) -> Tuple[str, dict]:
    """Returns (label comparable with Claude's, extra fields)."""
    th = s["thresholds"]
    if point == "collect_triage":
        sig = answers.get("signal", {})
        p_noise = prob(sig, "noise") + prob(sig, "expected")
        sev = answers.get("severity", {})
        label = "noise" if p_noise >= th["noise"] else "keep"
        return label, {"p_noise": round(p_noise, 3), "choice": sig.get("choice"),
                       "severity": sev.get("score"), "confidence": sig.get("confidence")}
    if point == "card_lint":
        p = float(answers.get("criteria", {}).get("noul", 0.0))
        diff = answers.get("difficulty", {})
        t = tier(float(diff.get("score") or 0), 2)
        return ("pass" if p >= th["criteria"] else "fail"), {
            "p_criteria": round(p, 3), "difficulty": t}
    if point == "risk_veto":
        p = float(answers.get("sensitive", {}).get("noul", 0.0))
        return ("hold" if p >= th["sensitive"] else "ok"), {"p_sensitive": round(p, 3)}
    if point == "bounce_reason":
        ans = answers.get("reason", {})
        return str(ans.get("choice") or "unknown"), {"confidence": ans.get("confidence")}
    raise ValueError(point)


def decide(cfg: dict, point: str, state: Any, item_id: str) -> dict:
    s = settings(cfg)
    mode = s["points"][point] if s["enabled"] else "off"
    base = {"point": point, "id": item_id, "mode": mode}
    if mode == "off":
        return {**base, "available": False, "reason": "disabled"}
    entries = read_log()
    spent = spent_this_month(entries)
    if spent >= float(s["monthly_budget_usd"]):
        return {**base, "available": False, "reason": f"monthly budget reached (${spent:.4f})"}
    try:
        resp = ask(s, state, QUESTIONS[point])
    except JevError as e:
        return {**base, "available": False, "reason": str(e)}
    label, extra = interpret(point, resp["answers"], s)
    cost = cost_of(resp)
    append_log({"kind": "jev", "point": point, "id": item_id, "mode": mode, "label": label,
                "cost": cost, "answers": resp["answers"], **extra})
    return {**base, "available": True, "label": label, "act": mode == "live", "cost": cost,
            **extra, "answers": resp["answers"]}


# --- shadow comparison -------------------------------------------------------

def report(entries: List[dict]) -> dict:
    jev: Dict[Tuple[str, str], dict] = {}
    claude: Dict[Tuple[str, str], str] = {}
    for e in entries:
        k = (e.get("point"), str(e.get("id")))
        if e.get("kind") == "jev":
            jev[k] = e
        elif e.get("kind") == "claude":
            claude[k] = e.get("label")
    out = {"spent_this_month_usd": round(spent_this_month(entries), 6), "points": {}}
    for point in POINTS:
        pairs = [(jev[k]["label"], claude[k]) for k in jev if k[0] == point and k in claude]
        agree = sum(1 for a, b in pairs if a == b)
        confusion: Dict[str, int] = {}
        for a, b in pairs:
            confusion[f"jev={a} claude={b}"] = confusion.get(f"jev={a} claude={b}", 0) + 1
        n = len(pairs)
        out["points"][point] = {
            "jev_decisions": sum(1 for k in jev if k[0] == point),
            "compared": n,
            "agreement": round(agree / n, 3) if n else None,
            "confusion": confusion,
            "advice": advice(point, n, agree, confusion),
        }
    return out


def advice(point: str, n: int, agree: int, confusion: Dict[str, int]) -> str:
    if n < 30:
        return f"keep in shadow: {n}/30 comparisons so far"
    rate = agree / n
    # The costly mistakes differ per point: dropping real bugs, waving through risky merges.
    costly = {"collect_triage": "jev=noise claude=keep", "risk_veto": "jev=ok claude=hold",
              "card_lint": "jev=pass claude=fail"}.get(point)
    if costly and confusion.get(costly, 0) > 0:
        return f"keep in shadow: {confusion[costly]} costly disagreement(s) ({costly})"
    if rate >= 0.9:
        return f"ready for live ({rate:.0%} agreement over {n})"
    return f"keep in shadow ({rate:.0%} agreement over {n}); consider tuning thresholds"


# --- PR risk gate ------------------------------------------------------------

def pr_state(repo: str, pr: str) -> str:
    view = subprocess.run(["gh", "pr", "view", pr, "--repo", repo, "--json", "title,body,files"],
                          capture_output=True, text=True)
    diff = subprocess.run(["gh", "pr", "diff", pr, "--repo", repo], capture_output=True, text=True)
    if view.returncode != 0:
        raise JevError(f"gh pr view failed: {view.stderr.strip()[:200]}")
    meta = json.loads(view.stdout)
    files = ", ".join(f["path"] for f in meta.get("files") or [])
    return (f"PR title: {meta.get('title')}\n\nDescription:\n{meta.get('body') or ''}\n\n"
            f"Files changed: {files}\n\nDiff:\n{diff.stdout}")


def gate(cfg: dict, pr: str) -> int:
    try:
        state = pr_state(cfg["repo"], pr)
    except (JevError, ValueError) as e:
        print(json.dumps({"point": "risk_veto", "available": False, "reason": str(e)}))
        return 0
    result = decide(cfg, "risk_veto", state, f"pr-{pr}")
    result.pop("answers", None)
    print(json.dumps(result))
    if result.get("available") and result.get("act") and result.get("label") == "hold":
        return EXIT_HOLD
    return 0


# --- CLI ---------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="jev.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    d = sub.add_parser("decide")
    d.add_argument("point", choices=POINTS)
    d.add_argument("state", help="file with the state (text or JSON), or - for stdin")
    d.add_argument("--id", required=True, help="stable id: issue number, fingerprint, pr-N")
    g = sub.add_parser("gate")
    g.add_argument("pr")
    r = sub.add_parser("record")
    r.add_argument("point", choices=POINTS)
    r.add_argument("id")
    r.add_argument("label")
    sub.add_parser("report")
    args = p.parse_args(argv)
    cfg = load_config()

    if args.cmd == "check":
        s = settings(cfg)
        try:
            resp = ask(s, "factory connectivity check",
                       {"ok": {"type": "noul", "instructions": "Is this a connectivity check?"}})
        except JevError as e:
            print(json.dumps({"status": "unavailable", "error": str(e)}))
            return 2
        print(json.dumps({"status": "ok", "model": resp.get("model"), "cost": cost_of(resp),
                          "enabled": s["enabled"], "points": s["points"]}))
        return 0
    if args.cmd == "decide":
        raw = sys.stdin.read() if args.state == "-" else open(args.state).read()
        try:
            state: Any = json.loads(raw)
        except ValueError:
            state = raw
        out = decide(cfg, args.point, state, args.id)
        out.pop("answers", None)
        print(json.dumps(out))
        return 0
    if args.cmd == "gate":
        return gate(cfg, args.pr)
    if args.cmd == "record":
        append_log({"kind": "claude", "point": args.point, "id": args.id, "label": args.label})
        return 0
    if args.cmd == "report":
        print(json.dumps(report(read_log()), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
