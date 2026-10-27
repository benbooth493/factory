#!/usr/bin/env python3
"""collect.py — find problems and file them as Backlog cards.

Commands (run from the repo root):

  collect.py sources                        which sources are enabled, and how each runs
  collect.py check <source>                 read-only connection test
  collect.py fetch <source> [--since 14d]   print candidates (and raw material) as JSON
  collect.py dedupe <candidates.json>       classify each candidate against filed cards
  collect.py render <card.json>             print the issue body a card would get
  collect.py file <card.json>               create (or adopt) the issue and put it in Backlog

<source> is one of:
  sentry        unresolved Sentry issues
  posthog       PostHog exceptions, clicks, slow pages, failures, surveys, gaps
  github        open issues in the repo that are not on the board (adopted, not copied)
  prs           review and QA comments on recently merged PRs (the skill clusters them)
  architecture  run by the skill; fetch only gathers context for the finder

Exit codes: 0 ok, 1 bad usage or a GitHub error, 2 source unavailable
(missing token, network, auth, API error), 3 source disabled in config.
"Unavailable" is never reported as "nothing found".

Secrets come from the environment, or from a .env file at the repo root
that this script reads itself (so the agent never needs to see it):
  SENTRY_AUTH_TOKEN           scopes: event:read, project:read
  POSTHOG_PERSONAL_API_KEY    scopes: query:read

Config lives under "collect" in .claude/factory.json; see
examples/factory.json. Python standard library only (3.9+).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

EXIT_OK, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_DISABLED = 0, 1, 2, 3
COLLECTED_LABEL = "factory:collected"
TRIAGE_LABEL = "needs-triage"
FP_RE = re.compile(r"factory-fingerprint:\s*`?([a-z0-9:._-]+)`?", re.I)
USER_AGENT = "factory-collect/0.2 (+https://github.com/benbooth493/factory)"


class Unavailable(Exception):
    """The source could not be read. Never to be confused with 'no problems'."""


# --- config and secrets -----------------------------------------------------

def repo_root() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.getcwd()


def load_config() -> Dict[str, Any]:
    path = os.environ.get("FACTORY_CONFIG") or os.path.join(repo_root(), ".claude", "factory.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except OSError:
        raise SystemExit(f"collect: no config at {path}; run /factory:board onboard")


def read_dotenv(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.replace("export ", "", 1).strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                values[key] = val
    except OSError:
        pass
    return values


def secret(name: str) -> Optional[str]:
    if os.environ.get(name):
        return os.environ[name]
    root = repo_root()
    for fname in (".env.local", ".env"):
        val = read_dotenv(os.path.join(root, fname)).get(name)
        if val:
            return val
    return None


# --- time windows -----------------------------------------------------------

def parse_since(value: str, now: Optional[dt.datetime] = None) -> Tuple[int, str]:
    """'14d' | '12h' | '2w' | 'YYYY-MM-DD' -> (whole days, ISO start)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    m = re.fullmatch(r"(\d+)\s*([hdw])", value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": dt.timedelta(hours=n), "d": dt.timedelta(days=n), "w": dt.timedelta(weeks=n)}[unit]
    else:
        try:
            start = dt.datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            raise SystemExit(f"collect: --since must look like 14d, 12h, 2w or 2026-09-01 (got {value!r})")
        delta = now - start
    if delta.total_seconds() <= 0:
        raise SystemExit("collect: --since is in the future")
    days = max(1, -(-int(delta.total_seconds()) // 86400))
    return days, (now - delta).replace(microsecond=0).isoformat()


# --- http -------------------------------------------------------------------

def http_json(method: str, url: str, headers: Dict[str, str],
              body: Optional[dict] = None, timeout: int = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        hint = {401: "token rejected", 403: "token lacks a needed scope", 404: "wrong org, project or host",
                429: "rate limited"}.get(e.code, "API error")
        raise Unavailable(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc} ({hint}): {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Unavailable(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {e}")
    except ValueError:
        raise Unavailable(f"non-JSON response from {urllib.parse.urlsplit(url).netloc}")


# The HTTP function is swappable so tests can replay recorded payloads.
HTTP: Callable[..., Any] = http_json


def short_hash(*parts: Any) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12]


def candidate(source: str, kind: str, fingerprint: str, title: str, **extra: Any) -> Dict[str, Any]:
    base = {"source": source, "kind": kind, "fingerprint": fingerprint, "title": title[:200]}
    base.update(extra)
    return base


# --- Sentry -----------------------------------------------------------------

def sentry_settings(cfg: dict) -> dict:
    s = dict((cfg.get("collect") or {}).get("sentry") or {})
    s.setdefault("host", "https://sentry.io")
    s["host"] = s["host"].rstrip("/")
    s.setdefault("min_events", 5)
    s.setdefault("min_users", 1)
    s.setdefault("limit", 25)
    s.setdefault("environment", None)
    return s


def sentry_ready(cfg: dict) -> Tuple[dict, str]:
    s = sentry_settings(cfg)
    if s.get("enabled") is False or not s.get("org") or not s.get("project"):
        raise LookupError("sentry is not enabled (set collect.sentry.org and .project)")
    token = secret("SENTRY_AUTH_TOKEN")
    if not token:
        raise Unavailable("SENTRY_AUTH_TOKEN is not set (environment or .env)")
    return s, token


def sentry_check(cfg: dict) -> dict:
    s, token = sentry_ready(cfg)
    url = f"{s['host']}/api/0/projects/{q(s['org'])}/{q(s['project'])}/"
    proj = HTTP("GET", url, {"Authorization": f"Bearer {token}"})
    return {"ok": True, "detail": f"sentry project {proj.get('slug', s['project'])} in {s['org']}"}


def q(part: str) -> str:
    return urllib.parse.quote(str(part), safe="")


def in_app_frames(event: dict, limit: int = 5) -> List[str]:
    frames: List[str] = []
    for entry in event.get("entries") or []:
        if entry.get("type") != "exception":
            continue
        for value in (entry.get("data") or {}).get("values") or []:
            for fr in reversed(((value.get("stacktrace") or {}).get("frames")) or []):
                if fr.get("inApp"):
                    where = fr.get("filename") or fr.get("module") or "?"
                    frames.append(f"{where}:{fr.get('lineNo', '?')} in {fr.get('function') or '?'}")
    return frames[:limit]


def sentry_fetch(cfg: dict, days: int, since_iso: str) -> List[dict]:
    s, token = sentry_ready(cfg)
    auth = {"Authorization": f"Bearer {token}"}
    query = f"is:unresolved lastSeen:-{days}d"
    if s.get("environment"):
        query += f" environment:{s['environment']}"
    params = urllib.parse.urlencode({
        "query": query, "sort": "freq", "limit": s["limit"],
        "statsPeriod": "24h" if days <= 1 else "14d",
    })
    url = f"{s['host']}/api/0/projects/{q(s['org'])}/{q(s['project'])}/issues/?{params}"
    issues = HTTP("GET", url, auth) or []
    out = []
    for it in issues:
        count, users = int(it.get("count") or 0), int(it.get("userCount") or 0)
        if count < s["min_events"] or users < s["min_users"]:
            continue
        frames: List[str] = []
        try:
            ev = HTTP("GET", f"{s['host']}/api/0/organizations/{q(s['org'])}/issues/{q(it['id'])}/events/latest/", auth)
            frames = in_app_frames(ev or {})
        except Unavailable:
            pass  # the issue is still worth reporting without a stack
        meta = it.get("metadata") or {}
        out.append(candidate(
            "sentry", "bug", f"sentry:{it['id']}", it.get("title") or meta.get("type") or "Sentry issue",
            summary=meta.get("value") or it.get("culprit") or "",
            count=count, users=users, first_seen=it.get("firstSeen"), last_seen=it.get("lastSeen"),
            url=it.get("permalink"), level=it.get("level"),
            evidence={"short_id": it.get("shortId"), "culprit": it.get("culprit"), "frames": frames},
        ))
    return out


# --- PostHog ----------------------------------------------------------------

SAFE_NAME = re.compile(r"^[\w $.:/-]{1,100}$")


def posthog_settings(cfg: dict) -> dict:
    s = dict((cfg.get("collect") or {}).get("posthog") or {})
    s.setdefault("host", "https://us.posthog.com")
    s["host"] = s["host"].rstrip("/")
    s.setdefault("min_events", 10)
    s.setdefault("min_users", 2)
    s.setdefault("limit", 25)
    s.setdefault("slow_lcp_ms", 2500)
    s.setdefault("failure_events", [])
    s.setdefault("signals", ["exceptions", "rageclicks", "deadclicks", "slow_pages", "failures",
                             "surveys", "pageview_paths"])
    return s


def posthog_ready(cfg: dict) -> Tuple[dict, str]:
    s = posthog_settings(cfg)
    if s.get("enabled") is False or not s.get("project_id"):
        raise LookupError("posthog is not enabled (set collect.posthog.project_id)")
    key = secret("POSTHOG_PERSONAL_API_KEY")
    if not key:
        raise Unavailable("POSTHOG_PERSONAL_API_KEY is not set (environment or .env)")
    return s, key


def hogql(s: dict, key: str, query: str) -> List[Dict[str, Any]]:
    url = f"{s['host']}/api/projects/{q(s['project_id'])}/query/"
    resp = HTTP("POST", url, {"Authorization": f"Bearer {key}"},
                {"query": {"kind": "HogQLQuery", "query": query}, "name": "factory-collect"})
    if not isinstance(resp, dict) or "results" not in resp:
        raise Unavailable(f"unexpected PostHog response: {str(resp)[:200]}")
    cols = resp.get("columns") or []
    return [dict(zip(cols, row)) for row in resp["results"]]


def posthog_check(cfg: dict) -> dict:
    s, key = posthog_ready(cfg)
    rows = hogql(s, key, "SELECT count() AS n FROM events WHERE timestamp > now() - INTERVAL 1 DAY")
    n = rows[0].get("n") if rows else 0
    return {"ok": True, "detail": f"posthog project {s['project_id']}: {n} events in the last day"}


def ph_queries(s: dict, days: int) -> Dict[str, str]:
    window = f"timestamp > now() - INTERVAL {int(days)} DAY"
    mn, lim = int(s["min_events"]), int(s["limit"])
    exc_type = ("coalesce(toString(properties.$exception_type), "
                "JSONExtractString(toString(properties.$exception_list), 1, 'type'))")
    exc_msg = ("coalesce(toString(properties.$exception_message), "
               "JSONExtractString(toString(properties.$exception_list), 1, 'value'))")
    common = ("count() AS events, uniq(distinct_id) AS users, "
              "min(timestamp) AS first_seen, max(timestamp) AS last_seen, "
              "any(properties.$current_url) AS url")
    queries = {
        "exceptions": f"""
            SELECT ifNull(coalesce(nullIf(toString(properties.$exception_issue_id), ''),
                                   nullIf(toString(properties.$exception_fingerprint), ''),
                                   concat({exc_type}, ': ', substring({exc_msg}, 1, 80))), 'unknown') AS key,
                   any({exc_type}) AS type, any({exc_msg}) AS message, {common}
            FROM events WHERE event = '$exception' AND {window}
            GROUP BY key HAVING events >= {mn} ORDER BY events DESC LIMIT {lim}""",
        "rageclicks": f"""
            SELECT properties.$pathname AS path, substring(any(elements_chain), 1, 300) AS element, {common}
            FROM events WHERE event = '$rageclick' AND {window}
            GROUP BY path HAVING events >= {mn} ORDER BY events DESC LIMIT {lim}""",
        "deadclicks": f"""
            SELECT properties.$pathname AS path, substring(any(elements_chain), 1, 300) AS element, {common}
            FROM events WHERE event = '$dead_click' AND {window}
            GROUP BY path HAVING events >= {mn} ORDER BY events DESC LIMIT {lim}""",
        "slow_pages": f"""
            SELECT properties.$pathname AS path,
                   round(quantile(0.75)(toFloat(properties.$web_vitals_LCP_value))) AS lcp_p75, {common}
            FROM events
            WHERE event = '$web_vitals' AND properties.$web_vitals_LCP_value IS NOT NULL AND {window}
            GROUP BY path HAVING events >= {mn} AND lcp_p75 > {int(s['slow_lcp_ms'])}
            ORDER BY lcp_p75 DESC LIMIT {lim}""",
        "surveys": f"""
            SELECT toString(properties.$survey_name) AS survey,
                   toString(properties.$survey_response) AS response, timestamp
            FROM events
            WHERE event = 'survey sent' AND length(toString(properties.$survey_response)) > 3 AND {window}
            ORDER BY timestamp DESC LIMIT 200""",
        "pageview_paths": f"""
            SELECT properties.$pathname AS path, count() AS views
            FROM events WHERE event = '$pageview' AND {window}
            GROUP BY path ORDER BY views DESC LIMIT 500""",
    }
    names = [n for n in s["failure_events"] if isinstance(n, str) and SAFE_NAME.match(n)]
    if names:
        listed = ", ".join("'" + n.replace("'", "\\'") + "'" for n in names)
        queries["failures"] = f"""
            SELECT event, properties.$pathname AS path, {common}
            FROM events WHERE event IN ({listed}) AND {window}
            GROUP BY event, path HAVING events >= {mn} ORDER BY events DESC LIMIT {lim}"""
    return {k: v for k, v in queries.items() if k in s["signals"]}


def posthog_fetch(cfg: dict, days: int, since_iso: str) -> Tuple[List[dict], Dict[str, Any], Dict[str, str]]:
    """Returns (candidates, raw material for the skill to judge, per-signal errors)."""
    s, key = posthog_ready(cfg)
    min_users = int(s["min_users"])
    out: List[dict] = []
    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for signal, query in ph_queries(s, days).items():
        try:
            rows = hogql(s, key, query)
        except Unavailable as e:
            errors[signal] = str(e)
            continue
        if signal in ("surveys", "pageview_paths"):
            raw[signal] = rows
            continue
        for r in rows:
            if int(r.get("users") or 0) < min_users:
                continue
            stats = dict(count=int(r.get("events") or 0), users=int(r.get("users") or 0),
                         first_seen=r.get("first_seen"), last_seen=r.get("last_seen"),
                         page=strip_query(r.get("url")))
            if signal == "exceptions":
                title = f"{r.get('type') or 'Exception'}: {r.get('message') or ''}".strip(": ")
                out.append(candidate("posthog", "bug", f"posthog:exception:{short_hash(r.get('key'))}",
                                     title, summary=r.get("message") or "", **stats,
                                     evidence={"type": r.get("type"), "group": r.get("key")}))
            elif signal in ("rageclicks", "deadclicks"):
                what = "Rage clicks" if signal == "rageclicks" else "Dead clicks"
                out.append(candidate("posthog", "ux",
                                     f"posthog:{signal[:-1]}:{short_hash(r.get('path'), r.get('element'))}",
                                     f"{what} on {r.get('path') or 'unknown page'}",
                                     summary=f"{what.lower()} by {stats['users']} users", **stats,
                                     evidence={"path": r.get("path"), "element": r.get("element")}))
            elif signal == "slow_pages":
                out.append(candidate("posthog", "perf", f"posthog:slow:{short_hash(r.get('path'))}",
                                     f"Slow page: {r.get('path')} (LCP p75 {int(r.get('lcp_p75') or 0)} ms)",
                                     summary=f"LCP p75 {int(r.get('lcp_p75') or 0)} ms over "
                                             f"{s['slow_lcp_ms']} ms budget", **stats,
                                     evidence={"path": r.get("path"), "lcp_p75_ms": r.get("lcp_p75")}))
            elif signal == "failures":
                out.append(candidate("posthog", "bug",
                                     f"posthog:failure:{short_hash(r.get('event'), r.get('path'))}",
                                     f"{r.get('event')} on {r.get('path') or 'unknown page'}",
                                     summary=f"failure event {r.get('event')}", **stats,
                                     evidence={"event": r.get("event"), "path": r.get("path")}))
    if errors and not out and not raw:
        raise Unavailable("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return out, raw, errors


def strip_query(url: Any) -> Optional[str]:
    """Keep scheme, host and path only: query strings often carry personal data."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(str(url))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# --- GitHub reads (github, prs) ---------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"https?://[^\s)>\]]+")
MENTION_RE = re.compile(r"(?<![\w/@])@[A-Za-z0-9][A-Za-z0-9-]{0,38}(?![\w/@-])")


def gh_read(*args: str) -> str:
    """Read-only gh call. A failure means the source is unavailable, not empty."""
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as e:
        raise Unavailable(f"cannot run gh: {e}")
    if r.returncode != 0:
        raise Unavailable(f"gh {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


# Swappable so tests can replay recorded gh output.
GH: Callable[..., str] = gh_read


def gh_json(*args: str) -> Any:
    out = GH(*args)
    try:
        return json.loads(out or "null")
    except ValueError:
        raise Unavailable(f"gh {' '.join(args[:2])} returned non-JSON output")


def scrub(text: Any, limit: int = 1500) -> str:
    """Drop emails and URL query strings, then trim. Comments go into cards as quotes."""
    text = EMAIL_RE.sub("<email>", str(text or ""))
    text = MENTION_RE.sub("@someone", text)
    text = URL_RE.sub(lambda m: strip_query(m.group(0)) or "", text)
    return text if len(text) <= limit else text[:limit] + " …"


def source_block(cfg: dict, name: str) -> dict:
    return dict((cfg.get("collect") or {}).get(name) or {})


def need_repo(cfg: dict, source: str) -> str:
    if not cfg.get("repo"):
        raise LookupError(f"{source} needs `repo` in .claude/factory.json")
    return cfg["repo"]


def board_urls(cfg: dict) -> set:
    """Every issue/PR URL on the board. A partial read is unavailable, never 'not on board'."""
    data = gh_json("project", "item-list", str(cfg["project_number"]), "--owner", cfg["owner"],
                   "--format", "json", "--limit", "1000")
    items = (data or {}).get("items") or []
    total = (data or {}).get("totalCount")
    if isinstance(total, int) and total > len(items):
        raise Unavailable(f"board read returned {len(items)} of {total} items; cannot tell what is on it")
    return {((it.get("content") or {}).get("url") or "").rstrip("/") for it in items} - {""}


GITHUB_KINDS = {"bug": "bug", "regression": "bug", "enhancement": "feature", "feature": "feature",
                "performance": "perf", "perf": "perf", "ux": "ux", "refactor": "refactor",
                "tech-debt": "refactor"}


def github_settings(cfg: dict) -> dict:
    s = source_block(cfg, "github")
    s.setdefault("limit", 200)
    s.setdefault("skip_labels", ["wontfix", "question", "duplicate", "invalid"])
    return s


def github_ready(cfg: dict) -> Tuple[dict, str]:
    s = github_settings(cfg)
    if s.get("enabled") is False:
        raise LookupError("github is disabled (collect.github.enabled is false)")
    return s, need_repo(cfg, "github")


def github_check(cfg: dict) -> dict:
    _, repo = github_ready(cfg)
    n = len(board_urls(cfg))
    return {"ok": True, "detail": f"read {repo} and the board ({n} items)"}


def github_fetch(cfg: dict, days: int, since_iso: str) -> Tuple[List[dict], Dict[str, Any], Dict[str, str]]:
    """Open issues not on the board. --since is ignored: an old issue is still open work."""
    s, repo = github_ready(cfg)
    on_board = board_urls(cfg)
    limit = int(s["limit"])
    issues = gh_json("issue", "list", "--repo", repo, "--state", "open", "--limit", str(limit),
                     "--json", "number,title,body,labels,url,createdAt,updatedAt") or []
    skip = {str(x).lower() for x in s["skip_labels"]} | {COLLECTED_LABEL}
    out = []
    for it in issues:
        url = (it.get("url") or "").rstrip("/")
        names = [str(lb.get("name") if isinstance(lb, dict) else lb).lower() for lb in it.get("labels") or []]
        if "/pull/" in url or url in on_board or skip & set(names):
            continue
        kind = next((GITHUB_KINDS[n] for n in names if n in GITHUB_KINDS), "task")
        out.append(candidate(
            "github", kind, f"github:{it['number']}", it.get("title") or f"Issue #{it['number']}",
            number=it["number"], url=it.get("url"), adopt=True,
            summary=scrub(it.get("body"), 600), first_seen=it.get("createdAt"), last_seen=it.get("updatedAt"),
            evidence={"labels": names}))
    raw = {"truncated": len(issues) >= limit, "open_issues_read": len(issues)}
    return out, raw, {}


# --- prs: review and QA comments on merged PRs -------------------------------

BOUNCE_LABEL = "bounce:"
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
REASON_RE = re.compile(r"^\s*Reason:\s*([a-z_]+)", re.I | re.M)
TRIVIAL = re.compile(r"^\s*(lgtm|thanks?|thank you|done|fixed|\+1|nit:?|ok|👍)[.!]*\s*$", re.I)


def prs_settings(cfg: dict) -> dict:
    s = source_block(cfg, "prs")
    s.setdefault("limit", 30)            # merged PRs read per run
    s.setdefault("max_comments", 40)     # per PR, newest kept
    s.setdefault("body_chars", 1500)     # per comment
    s.setdefault("min_prs", 2)           # a pattern must recur across this many PRs
    return s


def prs_ready(cfg: dict) -> Tuple[dict, str]:
    s = prs_settings(cfg)
    if s.get("enabled") is False:
        raise LookupError("prs is disabled (collect.prs.enabled is false)")
    return s, need_repo(cfg, "prs")


def rest_budget() -> Tuple[int, int]:
    core = (gh_json("api", "rate_limit") or {}).get("resources", {}).get("core", {})
    return int(core.get("remaining", 0)), int(core.get("reset", 0))


def prs_check(cfg: dict) -> dict:
    _, repo = prs_ready(cfg)
    remaining, _ = rest_budget()
    return {"ok": True, "detail": f"read merged PRs in {repo}; {remaining} REST calls left this hour"}


def role_of(body: str, state: str = "") -> str:
    head = body.lstrip()
    if head.startswith("🧪 QA FAIL"):
        return "qa_fail"
    if head.startswith("🧪 QA PASS"):
        return "qa_pass"
    if head.startswith("✅ Review"):
        return "review_approved"
    if REASON_RE.search(body) or state == "CHANGES_REQUESTED":
        return "review_findings"
    return "comment"


def author_kind(user: Any) -> str:
    user = user or {}
    login = str(user.get("login") or "")
    return "bot" if user.get("type") == "Bot" or login.endswith("[bot]") else "human"


def pr_notes(repo: str, n: int, s: dict) -> List[dict]:
    """Conversation comments, review bodies and inline review comments for one PR."""
    calls = [("comment", f"repos/{repo}/issues/{n}/comments?per_page=100"),
             ("review", f"repos/{repo}/pulls/{n}/reviews?per_page=100"),
             ("inline", f"repos/{repo}/pulls/{n}/comments?per_page=100")]
    notes = []
    for where, path in calls:
        for c in gh_json("api", path) or []:
            body = (c.get("body") or "").strip()
            role = role_of(body, c.get("state") or "")
            if len(body) < 15 or TRIVIAL.match(body) or role == "qa_pass":
                continue
            note = {"where": where, "role": role, "author": author_kind(c.get("user")),
                    "at": c.get("submitted_at") or c.get("created_at"), "body": scrub(body, int(s["body_chars"]))}
            if c.get("path"):
                note["path"] = c["path"]
                note["line"] = c.get("line") or c.get("original_line")
            notes.append(note)
    notes.sort(key=lambda x: str(x.get("at") or ""))
    return notes[-int(s["max_comments"]):]


def prs_fetch(cfg: dict, days: int, since_iso: str) -> Tuple[List[dict], Dict[str, Any], Dict[str, str]]:
    """Raw material only: the skill clusters it into patterns that recur across PRs."""
    s, repo = prs_ready(cfg)
    limit = int(s["limit"])
    prs = gh_json("pr", "list", "--repo", repo, "--state", "merged", "--search", f"merged:>={since_iso[:10]}",
                  "--limit", str(limit), "--json", "number,title,url,mergedAt,body,labels") or []
    remaining, reset = rest_budget()
    affordable = max(0, (remaining - 50) // 3)    # 3 REST calls per PR, keep a margin
    truncated = len(prs) >= limit
    if not prs:
        return [], {"prs": [], "truncated": False, "bounce_reasons": {}}, {}
    if affordable == 0:
        when = time.strftime("%H:%M UTC", time.gmtime(reset)) if reset else "later"
        raise Unavailable(f"GitHub REST budget too low ({remaining} left); try again after {when}")
    if affordable < len(prs):
        prs, truncated = prs[:affordable], True
    bounces = {}
    try:
        recent = gh_json("issue", "list", "--repo", repo, "--state", "all", "--search", f"updated:>={since_iso[:10]}",
                         "--limit", "300", "--json", "number,labels") or []
        for it in recent:
            names = [str(lb.get("name") if isinstance(lb, dict) else lb) for lb in it.get("labels") or []]
            bounces[it["number"]] = [x for x in names if x.startswith(BOUNCE_LABEL)]
    except Unavailable:
        pass  # bounce labels are a bonus; the comments still stand
    shaped, errors, reasons = [], {}, {}
    for pr in prs:
        try:
            notes = pr_notes(repo, pr["number"], s)
        except Unavailable as e:
            errors[f"pr#{pr['number']}"] = str(e)
            continue
        linked = sorted({int(x) for x in CLOSES_RE.findall(pr.get("body") or "")})
        labels = [str(lb.get("name") if isinstance(lb, dict) else lb) for lb in pr.get("labels") or []]
        blabels = sorted({x for x in labels if x.startswith(BOUNCE_LABEL)}
                         | {x for i in linked for x in bounces.get(i, [])})
        for lb in blabels:
            reasons.setdefault(lb[len(BOUNCE_LABEL):], []).append(pr["number"])
        shaped.append({"number": pr["number"], "title": pr.get("title"), "url": pr.get("url"),
                       "merged_at": pr.get("mergedAt"), "linked_issues": linked,
                       "bounce_labels": blabels, "notes": notes})
    if errors and not shaped:
        raise Unavailable("; ".join(f"{k}: {v}" for k, v in errors.items()))
    raw = {"prs": shaped, "truncated": truncated, "min_prs": int(s["min_prs"]),
           "bounce_reasons": {k: sorted(set(v)) for k, v in reasons.items()}}
    return [], raw, errors


# --- architecture: run by the skill; the script only gathers context -----------

def architecture_settings(cfg: dict) -> dict:
    s = source_block(cfg, "architecture")
    s.setdefault("max_findings", 5)
    return s


def architecture_ready(cfg: dict) -> dict:
    s = architecture_settings(cfg)
    if s.get("enabled") is False:
        raise LookupError("architecture is disabled (collect.architecture.enabled is false)")
    return s


def architecture_check(cfg: dict) -> dict:
    architecture_ready(cfg)
    return {"ok": True, "detail": f"runs as a read-only finder in {repo_root()}"}


def architecture_fetch(cfg: dict, days: int, since_iso: str) -> Tuple[List[dict], Dict[str, Any], Dict[str, str]]:
    s = architecture_ready(cfg)
    root = repo_root()
    adr_dir = os.path.join(root, "docs", "adr")
    adrs = sorted(f for f in os.listdir(adr_dir) if f.endswith(".md")) if os.path.isdir(adr_dir) else []
    churn: Dict[str, int] = {}
    try:
        log = subprocess.run(["git", "-C", root, "log", f"--since={since_iso}", "--name-only", "--format="],
                             capture_output=True, text=True, check=True).stdout
        for line in log.splitlines():
            if line.strip():
                churn[line.strip()] = churn.get(line.strip(), 0) + 1
    except (OSError, subprocess.CalledProcessError):
        pass  # not a git checkout: the finder still works, without the recency hint
    hot = sorted(churn.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    raw = {"runner": "agent", "max_findings": int(s["max_findings"]),
           "context_md": os.path.isfile(os.path.join(root, "CONTEXT.md")),
           "adrs": [os.path.join("docs", "adr", f) for f in adrs],
           "recently_changed": [{"path": p, "commits": n} for p, n in hot]}
    return [], raw, {}


# --- GitHub: dedupe and filing ----------------------------------------------

def gh(*args: str, input_text: Optional[str] = None) -> str:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, input=input_text)
    except OSError as e:
        raise SystemExit(f"collect: cannot run gh: {e}")
    if r.returncode != 0:
        raise SystemExit(f"collect: gh {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def filed_cards(repo: str) -> List[dict]:
    raw = gh("issue", "list", "--repo", repo, "--label", COLLECTED_LABEL, "--state", "all",
             "--limit", "1000", "--json", "number,state,closedAt,body,url,title")
    cards = []
    for issue in json.loads(raw or "[]"):
        m = FP_RE.search(issue.get("body") or "")
        if m:
            issue["fingerprint"] = m.group(1).lower()
        elif issue.get("number"):
            # An adopted issue keeps its own body; its fingerprint is its number.
            issue["fingerprint"] = f"github:{issue['number']}"
        else:
            continue
        cards.append(issue)
    return cards


def to_dt(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", text)
        if not m:
            return None
        parsed = dt.datetime.fromisoformat(m.group(1))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


SLUG_PREFIX = {"prs": "prs:", "architecture": "arch:"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FILE_LINE = re.compile(r"^[^\s:]+:\d+")


def problems(cand: dict, min_prs: int = 2) -> List[str]:
    """Rules the skill-built candidates must meet before they are verified or filed."""
    src, ev = cand.get("source"), cand.get("evidence") or {}
    found = []
    prefix, fp = SLUG_PREFIX.get(src), str(cand.get("fingerprint", ""))
    if prefix and not (fp.startswith(prefix) and SLUG_RE.match(fp[len(prefix):])):
        found.append(f"fingerprint must look like {prefix}<kebab-slug>")
    if src == "prs":
        if cand.get("kind") not in ("process", "bug"):
            found.append("prs kind must be process or bug")
        if len({p.get("number") for p in ev.get("prs") or []}) < min_prs:
            found.append(f"a prs pattern must recur across {min_prs}+ PRs")
    if src == "architecture":
        if cand.get("kind") != "refactor":
            found.append("architecture kind must be refactor")
        if not any(FILE_LINE.match(str(f)) for f in ev.get("files") or []):
            found.append("architecture evidence needs at least one file:line")
    return found


def classify(candidates: List[dict], cards: List[dict], min_prs: int = 2) -> List[dict]:
    """new | duplicate (an open card exists) | regressed (closed card, seen again since) | resolved."""
    by_fp: Dict[str, dict] = {}
    for c in cards:
        prev = by_fp.get(c["fingerprint"])
        if prev is None or (prev["state"] != "OPEN" and c["state"] == "OPEN"):
            by_fp[c["fingerprint"]] = c
    out = []
    for cand in candidates:
        match = by_fp.get(cand["fingerprint"].lower())
        item = dict(cand)
        bad = problems(cand, min_prs)
        if bad:
            item.update(status="invalid", reasons=bad)
        elif not match:
            item["status"] = "new"
        elif match["state"] == "OPEN":
            item.update(status="duplicate", existing=match["number"])
        elif (to_dt(cand.get("last_seen")) and to_dt(match.get("closedAt"))
              and to_dt(cand.get("last_seen")) > to_dt(match.get("closedAt"))):
            item.update(status="regressed", existing=match["number"])
        else:
            item.update(status="resolved", existing=match["number"])
        out.append(item)
    return out


def fmt_n(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n or "?")


DEFAULT_CRITERIA = {
    "bug": ["The failure no longer reproduces with the steps below",
            "A test reproduces the original failure and now passes",
            "No new errors with this fingerprint after the fix is deployed"],
    "ux": ["The element responds visibly on the first click",
           "A test or recorded check covers the interaction"],
    "tracking": ["The route sends a $pageview (or the named event) to PostHog",
                 "A test or recorded manual check confirms the event fires once per visit"],
    "perf": ["LCP p75 for this page is under budget in a production-like build",
             "The cause of the slowness is identified in the PR description"],
    "process": ["The rule or check that would have caught this is written down where the lanes read it "
                "(CLAUDE.md, a lint rule, a test helper or a skill)",
                "A test or check fails on the pattern quoted in the evidence and passes after the change",
                "The next merged PRs show no review or QA comment of this shape"],
    "refactor": ["Behaviour is unchanged: no public interface, output or test expectation changes",
                 "The existing test suite still passes, unchanged",
                 "The simplification is measurable: the PR states the before/after count "
                 "(lines, files, modules or call sites) for the code named in the evidence"],
    "feature": ["The behaviour described above works end to end",
                "A test covers it"],
    "task": ["The outcome described above is done and visible",
             "A test or recorded check shows it"],
}


SOURCE_NAMES = {"sentry": "Sentry", "posthog": "PostHog", "github": "GitHub issues",
                "prs": "merged PR reviews", "architecture": "an architecture review"}


def render(card: dict) -> str:
    """Issue body for a verified candidate. `card` = candidate + verifier fields."""
    src = SOURCE_NAMES.get(card["source"], card["source"].capitalize())
    lines = [f"**Found by factory collect** from {src}"
             + (f" ([source]({card['url']}))" if card.get("url") else "") + "."]
    if card.get("verdict") == "unclear":
        lines.append("\n> ⚠️ The verifier could not confirm this. It needs a human look before Ready.")
    lines += ["", "## What is happening", "", card.get("what") or card.get("summary") or card["title"], ""]
    lines += ["## Evidence", ""]
    if card.get("count") is not None or card.get("users") is not None:
        lines.append(f"- **{fmt_n(card.get('count'))}** events from **{fmt_n(card.get('users'))}** users")
    if card.get("first_seen") or card.get("last_seen"):
        lines.append(f"- First seen {card.get('first_seen') or '?'}, last seen {card.get('last_seen') or '?'}")
    ev = card.get("evidence") or {}
    for pr in ev.get("prs") or []:
        quote = f": “{scrub(pr.get('quote'), 200)}”" if pr.get("quote") else ""
        lines.append(f"- PR #{pr.get('number')}" + (f" ({pr['role']})" if pr.get("role") else "") + quote)
    for loc in ev.get("files") or []:
        lines.append(f"- `{loc}`")
    if ev.get("short_id"):
        lines.append(f"- Sentry {ev['short_id']}" + (f" in `{ev['culprit']}`" if ev.get("culprit") else ""))
    for frame in ev.get("frames") or []:
        lines.append(f"  - `{frame}`")
    if ev.get("path"):
        lines.append(f"- Page: `{ev['path']}`")
    elif card.get("page"):
        lines.append(f"- Page: `{card['page']}`")
    if ev.get("element"):
        lines.append(f"- Element: `{str(ev['element'])[:200]}`")
    if ev.get("lcp_p75_ms"):
        lines.append(f"- LCP p75: {fmt_n(ev['lcp_p75_ms'])} ms")
    for note in card.get("notes") or []:
        lines.append(f"- {note}")
    if card.get("cause"):
        lines += ["", "## Likely cause", "", card["cause"]]
    if card.get("repro"):
        lines += ["", "## Steps to reproduce", ""]
        lines += [f"{i}. {step}" for i, step in enumerate(card["repro"], 1)]
    criteria = card.get("acceptance") or DEFAULT_CRITERIA.get(card.get("kind", "bug"), DEFAULT_CRITERIA["bug"])
    lines += ["", "## Acceptance criteria", ""] + [f"- [ ] {c}" for c in criteria]
    lines += ["", f"<sub>factory-fingerprint: `{card['fingerprint']}`</sub>"]
    return "\n".join(lines) + "\n"


def adoption_note(card: dict) -> str:
    """Comment left on an adopted issue: the issue body stays the user's own."""
    lines = ["📥 **Adopted by factory collect** into Backlog."]
    if card.get("verdict") == "unclear":
        lines.append("\n> ⚠️ The verifier could not confirm this. It needs a human look before Ready.")
    if card.get("what"):
        lines += ["", card["what"]]
    if card.get("cause"):
        lines += ["", f"**Likely cause:** {card['cause']}"]
    if card.get("acceptance"):
        lines += ["", "**Suggested acceptance criteria** (copy into the issue if they fit):", ""]
        lines += [f"- [ ] {c}" for c in card["acceptance"]]
    lines += ["", f"<sub>factory-fingerprint: `{card['fingerprint']}`</sub>"]
    return "\n".join(lines) + "\n"


def labels_for(card: dict) -> List[str]:
    labels = [COLLECTED_LABEL, f"source:{card['source']}", f"kind:{card.get('kind', 'bug')}"]
    if card.get("verdict") == "unclear":
        labels.append(TRIAGE_LABEL)
    return labels


def file_card(cfg: dict, card: dict) -> dict:
    repo = cfg["repo"]
    colors = {COLLECTED_LABEL: "0e8a16", TRIAGE_LABEL: "fbca04"}
    for label in labels_for(card):
        subprocess.run(["gh", "label", "create", label, "--repo", repo, "--color",
                        colors.get(label, "c5def5"), "--description", "Managed by factory collect"],
                       capture_output=True)
    if card.get("adopt"):
        n = str(card["number"])
        gh("issue", "edit", n, "--repo", repo, *[a for lb in labels_for(card) for a in ("--add-label", lb)])
        gh("issue", "comment", n, "--repo", repo, "--body-file", "-", input_text=adoption_note(card))
        url = card.get("url") or json.loads(gh("issue", "view", n, "--repo", repo, "--json", "url"))["url"]
        number = int(n)
    elif card.get("status") == "regressed" and card.get("existing"):
        n = str(card["existing"])
        gh("issue", "reopen", n, "--repo", repo)
        gh("issue", "comment", n, "--repo", repo, "--body-file", "-",
           input_text=f"🔁 **Regressed**: seen again after this was closed.\n\n{render(card)}")
        url = json.loads(gh("issue", "view", n, "--repo", repo, "--json", "url"))["url"]
        number = int(n)
    else:
        args = ["issue", "create", "--repo", repo, "--title", card["title"], "--body-file", "-"]
        for label in labels_for(card):
            args += ["--label", label]
        url = gh(*args, input_text=render(card)).strip().splitlines()[-1]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    gh("project", "item-add", str(cfg["project_number"]), "--owner", cfg["owner"], "--url", url)
    board = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.sh")
    subprocess.run(["bash", board, "move", str(number), "backlog"], check=False,
                   stdout=subprocess.DEVNULL)
    status = "adopted" if card.get("adopt") else card.get("status", "new")
    return {"number": number, "url": url, "status": status}


# --- CLI --------------------------------------------------------------------

SOURCES = {"sentry": (sentry_check, sentry_fetch), "posthog": (posthog_check, posthog_fetch),
           "github": (github_check, github_fetch), "prs": (prs_check, prs_fetch),
           "architecture": (architecture_check, architecture_fetch)}
OPT_IN = ("github", "prs", "architecture")  # run by default only with enabled: true


def default_sources(cfg: dict) -> List[str]:
    """Sources a bare /factory:collect runs."""
    picked = []
    for name in SOURCES:
        block = source_block(cfg, name)
        if block.get("enabled") is False:
            continue
        if name in OPT_IN:
            if block.get("enabled") is True:
                picked.append(name)
        elif block:
            picked.append(name)
    return picked


def emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def run_source(cmd: str, source: str, since: str) -> int:
    cfg = load_config()
    check, fetch = SOURCES[source]
    try:
        if cmd == "check":
            emit({"source": source, "status": "ok", **check(cfg)})
            return EXIT_OK
        since = since or (cfg.get("collect") or {}).get("since") or "14d"
        days, since_iso = parse_since(since)
        result = fetch(cfg, days, since_iso)
        if isinstance(result, tuple):
            cands, raw, errors = result
        else:
            cands, raw, errors = result, {}, {}
        emit({"source": source, "status": "ok", "since": since_iso, "days": days,
              "candidates": cands, "raw": raw, "partial_errors": errors})
        return EXIT_OK
    except LookupError as e:
        emit({"source": source, "status": "disabled", "error": str(e)})
        return EXIT_DISABLED
    except Unavailable as e:
        emit({"source": source, "status": "unavailable", "error": str(e)})
        return EXIT_UNAVAILABLE


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="collect.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources")
    for name in ("check", "fetch"):
        sp = sub.add_parser(name)
        sp.add_argument("source", choices=sorted(SOURCES))
        if name == "fetch":
            sp.add_argument("--since", default="")
    for name in ("dedupe", "render", "file"):
        sp = sub.add_parser(name)
        sp.add_argument("path", help="JSON file, or - for stdin")
    args = p.parse_args(argv)

    if args.cmd == "sources":
        cfg = load_config()
        emit({"default": default_sources(cfg), "all": list(SOURCES),
              "agent_run": ["architecture"], "adopts": ["github"], "raw_only": ["prs", "architecture"]})
        return EXIT_OK
    if args.cmd in ("check", "fetch"):
        return run_source(args.cmd, args.source, getattr(args, "since", ""))

    if args.path == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.path) as fh:
            data = json.load(fh)
    if args.cmd == "render":
        sys.stdout.write(render(data))
        return EXIT_OK
    cfg = load_config()
    if args.cmd == "dedupe":
        cands = data.get("candidates", data) if isinstance(data, dict) else data
        arch = [c for c in cands if c.get("source") == "architecture"]
        limit = int(architecture_settings(cfg)["max_findings"])
        out = classify(cands, filed_cards(cfg["repo"]), int(prs_settings(cfg)["min_prs"]))
        for item in [o for o in out if o.get("source") == "architecture"][limit:]:
            item.update(status="invalid", reasons=[f"over collect.architecture.max_findings ({limit})"])
        emit({"candidates": out, "architecture_proposed": len(arch)})
        return EXIT_OK
    if args.cmd == "file":
        cards = data if isinstance(data, list) else [data]
        min_prs = int(prs_settings(cfg)["min_prs"])
        bad = {c.get("fingerprint"): problems(c, min_prs) for c in cards if problems(c, min_prs)}
        if bad:
            emit({"error": "refusing to file invalid candidates", "invalid": bad})
            return EXIT_USAGE
        emit({"filed": [file_card(cfg, c) for c in cards]})
        return EXIT_OK
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
