"""Offline tests for scripts/collect.py. Replays canned Sentry and PostHog payloads."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import collect  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "collect")


def fixture(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


CFG = {
    "owner": "acme", "project_number": 5, "repo": "acme/app",
    "collect": {
        "sentry": {"org": "acme", "project": "web", "min_events": 5},
        "posthog": {"project_id": 42, "host": "https://eu.posthog.com", "min_events": 10,
                    "min_users": 2, "failure_events": ["checkout_failed", "bad'; DROP"]},
    },
}


class FakeHTTP:
    """Routes requests to fixtures and records them."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, method, url, headers, body=None, timeout=30):
        self.calls.append((method, url, headers, body))
        if any(f in url or (body and f in body["query"]["query"]) for f in self.fail_on):
            raise collect.Unavailable("HTTP 403 (token lacks a needed scope)")
        if "/events/latest/" in url:
            return fixture("sentry_latest_event.json")
        if "/issues/" in url:
            return fixture("sentry_issues.json")
        if "/api/0/projects/" in url:
            return {"slug": "web"}
        if "/query/" in url:
            q = body["query"]["query"]
            for key in ("$exception", "$rageclick", "$dead_click", "$web_vitals", "survey sent",
                        "$pageview", "checkout_failed", "INTERVAL 1 DAY"):
                if key in q:
                    return fixture("posthog.json")[key]
        raise AssertionError(f"unexpected request {method} {url}")


class Base(unittest.TestCase):
    def setUp(self):
        self.http = FakeHTTP()
        self._orig = collect.HTTP
        collect.HTTP = self.http
        os.environ["SENTRY_AUTH_TOKEN"] = "test-sentry"
        os.environ["POSTHOG_PERSONAL_API_KEY"] = "test-posthog"
        self.addCleanup(self.restore)

    def restore(self):
        collect.HTTP = self._orig
        os.environ.pop("SENTRY_AUTH_TOKEN", None)
        os.environ.pop("POSTHOG_PERSONAL_API_KEY", None)


class SinceTest(unittest.TestCase):
    now = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)

    def test_relative(self):
        self.assertEqual(collect.parse_since("14d", self.now)[0], 14)
        self.assertEqual(collect.parse_since("2w", self.now)[0], 14)
        self.assertEqual(collect.parse_since("12h", self.now)[0], 1)

    def test_date(self):
        days, start = collect.parse_since("2026-10-01", self.now)
        self.assertEqual(days, 27)
        self.assertTrue(start.startswith("2026-10-01"))

    def test_bad(self):
        with self.assertRaises(SystemExit):
            collect.parse_since("soon", self.now)
        with self.assertRaises(SystemExit):
            collect.parse_since("2027-01-01", self.now)


class SentryTest(Base):
    def test_fetch_filters_and_shapes(self):
        cands = collect.sentry_fetch(CFG, 14, "x")
        self.assertEqual([c["fingerprint"] for c in cands], ["sentry:101", "sentry:102"])
        first = cands[0]
        self.assertEqual(first["count"], 840)
        self.assertEqual(first["users"], 120)
        self.assertEqual(first["kind"], "bug")
        self.assertEqual(first["evidence"]["frames"][0], "src/cart/total.ts:42 in computeTotal")
        self.assertEqual(len(first["evidence"]["frames"]), 2, "only in-app frames")

    def test_query_uses_window_and_bearer(self):
        collect.sentry_fetch(CFG, 30, "x")
        method, url, headers, _ = self.http.calls[0]
        self.assertIn("lastSeen%3A-30d", url)
        self.assertIn("is%3Aunresolved", url)
        self.assertEqual(headers["Authorization"], "Bearer test-sentry")

    def test_latest_event_failure_still_reports(self):
        self.http.fail_on = ("/events/latest/",)
        cands = collect.sentry_fetch(CFG, 14, "x")
        self.assertEqual(len(cands), 2)
        self.assertEqual(cands[0]["evidence"]["frames"], [])


class PostHogTest(Base):
    def test_fetch_all_signals(self):
        cands, raw, errors = collect.posthog_fetch(CFG, 14, "x")
        kinds = sorted({c["fingerprint"].split(":")[1] for c in cands})
        self.assertEqual(kinds, ["deadclick", "exception", "failure", "rageclick", "slow"])
        self.assertEqual(errors, {})
        self.assertEqual(len(raw["surveys"]), 2)
        self.assertIn("/pricing", [r["path"] for r in raw["pageview_paths"]])

    def test_min_users_filter(self):
        cands, _, _ = collect.posthog_fetch(CFG, 14, "x")
        titles = " ".join(c["title"] for c in cands)
        self.assertNotIn("/lonely", titles)

    def test_fingerprints_are_stable(self):
        a, _, _ = collect.posthog_fetch(CFG, 14, "x")
        b, _, _ = collect.posthog_fetch(CFG, 14, "x")
        self.assertEqual([c["fingerprint"] for c in a], [c["fingerprint"] for c in b])

    def test_no_query_strings_leak(self):
        cands, _, _ = collect.posthog_fetch(CFG, 14, "x")
        pricing = [c for c in cands if c["evidence"].get("path") == "/pricing"][0]
        self.assertEqual(pricing["page"], "https://app.acme.dev/pricing")
        self.assertNotIn("email", json.dumps(cands))
        self.assertNotIn("email", collect.render(pricing))

    def test_failure_event_names_are_sanitised(self):
        queries = collect.ph_queries(collect.posthog_settings(CFG), 14)
        self.assertIn("'checkout_failed'", queries["failures"])
        self.assertNotIn("DROP", queries["failures"])

    def test_partial_errors_reported(self):
        self.http.fail_on = ("$web_vitals",)
        cands, _, errors = collect.posthog_fetch(CFG, 14, "x")
        self.assertIn("slow_pages", errors)
        self.assertTrue(cands)

    def test_everything_failing_is_unavailable(self):
        self.http.fail_on = ("/query/",)
        with self.assertRaises(collect.Unavailable):
            collect.posthog_fetch(CFG, 14, "x")


class CliTest(Base):
    def run_cli(self, *argv, cfg=CFG):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
        os.environ["FACTORY_CONFIG"] = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(os.environ.pop, "FACTORY_CONFIG", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = collect.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_fetch_ok(self):
        code, out = self.run_cli("fetch", "sentry", "--since", "7d")
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["days"], 7)

    def test_missing_token_is_unavailable_not_empty(self):
        os.environ.pop("SENTRY_AUTH_TOKEN")
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)  # no .env here, not a git repo
            try:
                code, out = self.run_cli("fetch", "sentry")
            finally:
                os.chdir(cwd)
        self.assertEqual(code, collect.EXIT_UNAVAILABLE)
        self.assertEqual(out["status"], "unavailable")
        self.assertNotIn("candidates", out)

    def test_api_error_is_unavailable(self):
        self.http.fail_on = ("/issues/",)
        code, out = self.run_cli("fetch", "sentry")
        self.assertEqual(code, collect.EXIT_UNAVAILABLE)
        self.assertIn("403", out["error"])

    def test_disabled(self):
        cfg = dict(CFG, collect={"posthog": {"enabled": False, "project_id": 1}})
        code, out = self.run_cli("fetch", "posthog", cfg=cfg)
        self.assertEqual(code, collect.EXIT_DISABLED)
        self.assertEqual(out["status"], "disabled")

    def test_check(self):
        code, out = self.run_cli("check", "posthog")
        self.assertEqual(code, 0)
        self.assertIn("1234 events", out["detail"])


class DotenvTest(unittest.TestCase):
    def test_reads_values_without_exposing(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("# comment\nexport A=1\nB=\"two words\"\nC='x'\nNOVALUE\n")
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(collect.read_dotenv(fh.name), {"A": "1", "B": "two words", "C": "x"})


class DedupeTest(unittest.TestCase):
    cards = [
        {"number": 1, "state": "OPEN", "closedAt": None, "fingerprint": "sentry:101"},
        {"number": 2, "state": "CLOSED", "closedAt": "2026-10-01T00:00:00Z", "fingerprint": "sentry:102"},
        {"number": 3, "state": "CLOSED", "closedAt": "2026-10-25T00:00:00Z", "fingerprint": "sentry:103"},
    ]

    def test_classify(self):
        cands = [
            {"fingerprint": "sentry:101", "last_seen": "2026-10-26T00:00:00Z"},
            {"fingerprint": "sentry:102", "last_seen": "2026-10-20T08:00:00.123+00:00"},
            {"fingerprint": "sentry:103", "last_seen": "2026-10-20T00:00:00Z"},
            {"fingerprint": "sentry:999", "last_seen": "2026-10-26T00:00:00Z"},
        ]
        got = {c["fingerprint"]: (c["status"], c.get("existing")) for c in collect.classify(cands, self.cards)}
        self.assertEqual(got["sentry:101"], ("duplicate", 1))
        self.assertEqual(got["sentry:102"], ("regressed", 2))
        self.assertEqual(got["sentry:103"], ("resolved", 3))
        self.assertEqual(got["sentry:999"], ("new", None))

    def test_open_card_wins_over_closed(self):
        cards = self.cards + [{"number": 9, "state": "OPEN", "closedAt": None, "fingerprint": "sentry:102"}]
        got = collect.classify([{"fingerprint": "sentry:102", "last_seen": "2026-10-26"}], cards)
        self.assertEqual((got[0]["status"], got[0]["existing"]), ("duplicate", 9))


class RenderTest(unittest.TestCase):
    card = {
        "source": "sentry", "kind": "bug", "fingerprint": "sentry:101", "title": "TypeError: x is undefined",
        "url": "https://sentry.io/i/101", "count": 840, "users": 120,
        "first_seen": "2026-10-01", "last_seen": "2026-10-26",
        "evidence": {"short_id": "WEB-1A", "culprit": "computeTotal", "frames": ["src/cart/total.ts:42 in computeTotal"]},
        "verdict": "real", "cause": "Empty cart has no items array.",
        "repro": ["Open /cart with an empty basket", "Click Checkout"],
    }

    def test_body(self):
        body = collect.render(self.card)
        self.assertIn("## Acceptance criteria", body)
        self.assertIn("- [ ] ", body)
        self.assertIn("**840** events from **120** users", body)
        self.assertIn("1. Open /cart", body)
        self.assertNotIn("needs a human", body)
        self.assertEqual(collect.FP_RE.search(body).group(1), "sentry:101", "fingerprint round-trips")

    def test_unclear_gets_triage(self):
        card = dict(self.card, verdict="unclear")
        self.assertIn("needs a human look", collect.render(card))
        self.assertIn("needs-triage", collect.labels_for(card))

    def test_custom_criteria(self):
        body = collect.render(dict(self.card, acceptance=["Cart total shows £0.00 when empty"]))
        self.assertIn("- [ ] Cart total shows £0.00 when empty", body)
        self.assertNotIn("A test reproduces", body)


# --- github, prs, architecture -------------------------------------------------

class FakeGH:
    """Answers read-only gh calls from fixtures."""

    def __init__(self, remaining=5000, fail_on=()):
        self.calls = []
        self.remaining = remaining
        self.fail_on = fail_on
        self.notes = fixture("prs_notes.json")

    def __call__(self, *args):
        self.calls.append(args)
        joined = " ".join(args)
        if any(f in joined for f in self.fail_on):
            raise collect.Unavailable(f"gh {args[0]} failed: HTTP 403")
        if args[:2] == ("project", "item-list"):
            return json.dumps(fixture("github_board.json"))
        if args[:2] == ("issue", "list") and "open" in args:
            return json.dumps(fixture("github_issues.json"))
        if args[:2] == ("issue", "list"):
            return json.dumps(fixture("prs_issues.json"))
        if args[:2] == ("pr", "list"):
            return json.dumps(fixture("prs_merged.json"))
        if args == ("api", "rate_limit"):
            return json.dumps({"resources": {"core": {"remaining": self.remaining, "reset": 1798000000}}})
        if args[0] == "api":
            parts = args[1].split("?")[0].split("/")
            n, kind = parts[-2], parts[-1]
            key = {"comments": "inline" if parts[-3] == "pulls" else "issues", "reviews": "reviews"}[kind]
            return json.dumps(self.notes[n][key])
        raise AssertionError(f"unexpected gh {joined}")


class GHBase(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGH()
        self._orig = collect.GH
        collect.GH = self.gh
        self.addCleanup(setattr, collect, "GH", self._orig)


class GitHubSourceTest(GHBase):
    def test_only_open_issues_not_on_board(self):
        cands, raw, errors = collect.github_fetch(CFG, 14, "x")
        self.assertEqual([c["number"] for c in cands], [12, 13, 17])
        self.assertEqual(errors, {})
        self.assertFalse(raw["truncated"])

    def test_candidates_are_adoptions_with_stable_fingerprints(self):
        cands, _, _ = collect.github_fetch(CFG, 14, "x")
        by_n = {c["number"]: c for c in cands}
        self.assertTrue(all(c["adopt"] for c in cands))
        self.assertEqual(by_n[12]["fingerprint"], "github:12")
        self.assertEqual((by_n[12]["kind"], by_n[13]["kind"], by_n[17]["kind"]), ("bug", "feature", "task"))

    def test_personal_data_scrubbed(self):
        cands, _, _ = collect.github_fetch(CFG, 14, "x")
        dumped = json.dumps(cands)
        self.assertNotIn("jo@example.com", dumped)
        self.assertNotIn("token=abc123", dumped)
        self.assertIn("https://app.acme.dev/export", dumped)

    def test_board_failure_is_unavailable(self):
        self.gh.fail_on = ("item-list",)
        with self.assertRaises(collect.Unavailable):
            collect.github_fetch(CFG, 14, "x")

    def test_partial_board_is_unavailable(self):
        orig = fixture("github_board.json")
        orig["totalCount"] = 900
        collect.GH = lambda *a: json.dumps(orig) if a[:2] == ("project", "item-list") else self.gh(*a)
        with self.assertRaises(collect.Unavailable):
            collect.github_fetch(CFG, 14, "x")

    def test_disabled(self):
        with self.assertRaises(LookupError):
            collect.github_fetch(dict(CFG, collect={"github": {"enabled": False}}), 14, "x")

    def test_adopted_issue_dedupes_by_number(self):
        cards = [{"number": 12, "state": "OPEN", "closedAt": None, "fingerprint": "github:12"}]
        got = collect.classify([{"source": "github", "kind": "bug", "fingerprint": "github:12"}], cards)
        self.assertEqual(got[0]["status"], "duplicate")


class PrsSourceTest(GHBase):
    def test_raw_material_only(self):
        cands, raw, errors = collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        self.assertEqual(cands, [], "the skill clusters patterns; the script never invents candidates")
        self.assertEqual([p["number"] for p in raw["prs"]], [31, 34, 36])
        self.assertEqual(raw["min_prs"], 2)
        self.assertEqual(errors, {})

    def test_roles_and_noise(self):
        _, raw, _ = collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        pr31 = raw["prs"][0]
        roles = [n["role"] for n in pr31["notes"]]
        self.assertIn("qa_fail", roles)
        self.assertIn("review_approved", roles)
        self.assertNotIn("qa_pass", roles, "passing QA reports are not problems")
        self.assertFalse(any(n["body"] == "LGTM" for n in pr31["notes"]))
        inline = [n for n in pr31["notes"] if n["where"] == "inline"][0]
        self.assertEqual((inline["author"], inline["path"], inline["line"]), ("bot", "src/reports/monthly.ts", 42))
        pr34 = raw["prs"][1]
        self.assertEqual(pr34["notes"][0]["role"], "review_findings")
        self.assertEqual(pr34["notes"][1]["line"], 18)

    def test_bounce_labels_from_pr_and_linked_issue(self):
        _, raw, _ = collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        self.assertEqual(raw["prs"][0]["linked_issues"], [21])
        self.assertEqual(raw["prs"][0]["bounce_labels"], ["bounce:logic_bug"])
        self.assertEqual(raw["bounce_reasons"], {"logic_bug": [31, 34]})

    def test_personal_data_scrubbed(self):
        _, raw, _ = collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        dumped = json.dumps(raw)
        self.assertNotIn("dana@acme.dev", dumped)
        self.assertNotIn("@dana", dumped)
        self.assertNotIn("diff=split", dumped)
        self.assertNotIn("login", dumped, "author logins are reduced to human/bot")

    def test_window_and_limit_reach_gh(self):
        cfg = dict(CFG, collect={"prs": {"limit": 3}})
        _, raw, _ = collect.prs_fetch(cfg, 14, "2026-10-13T00:00:00+00:00")
        pr_list = [c for c in self.gh.calls if c[:2] == ("pr", "list")][0]
        self.assertIn("merged:>=2026-10-13", pr_list)
        self.assertEqual(pr_list[pr_list.index("--limit") + 1], "3")
        self.assertTrue(raw["truncated"], "a full page means there may be more")

    def test_rate_limit_caps_volume(self):
        self.gh.remaining = 56   # (56-50)//3 = 2 PRs affordable
        _, raw, _ = collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        self.assertEqual(len(raw["prs"]), 2)
        self.assertTrue(raw["truncated"])

    def test_exhausted_budget_is_unavailable(self):
        self.gh.remaining = 10
        with self.assertRaises(collect.Unavailable) as e:
            collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
        self.assertIn("budget", str(e.exception))

    def test_comment_volume_capped(self):
        cfg = dict(CFG, collect={"prs": {"max_comments": 1, "body_chars": 20}})
        _, raw, _ = collect.prs_fetch(cfg, 14, "2026-10-13T00:00:00+00:00")
        self.assertEqual(len(raw["prs"][0]["notes"]), 1)
        self.assertLessEqual(len(raw["prs"][0]["notes"][0]["body"]), 22)

    def test_all_prs_failing_is_unavailable(self):
        self.gh.fail_on = ("repos/acme/app/",)
        with self.assertRaises(collect.Unavailable):
            collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")

    def test_pr_list_failure_is_unavailable(self):
        self.gh.fail_on = ("pr list",)
        with self.assertRaises(collect.Unavailable):
            collect.prs_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")


class ArchitectureSourceTest(unittest.TestCase):
    def test_gathers_context_without_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "docs", "adr"))
            open(os.path.join(d, "docs", "adr", "0001-use-postgres.md"), "w").close()
            open(os.path.join(d, "CONTEXT.md"), "w").close()
            cwd = os.getcwd()
            os.chdir(d)
            try:
                cands, raw, _ = collect.architecture_fetch(CFG, 14, "2026-10-13T00:00:00+00:00")
            finally:
                os.chdir(cwd)
        self.assertEqual(cands, [])
        self.assertEqual(raw["runner"], "agent")
        self.assertTrue(raw["context_md"])
        self.assertEqual(raw["adrs"], ["docs/adr/0001-use-postgres.md"])
        self.assertEqual(raw["max_findings"], 5)

    def test_disabled(self):
        with self.assertRaises(LookupError):
            collect.architecture_fetch(dict(CFG, collect={"architecture": {"enabled": False}}), 14, "x")


PRS_CAND = {"source": "prs", "kind": "bug", "fingerprint": "prs:date-only-strings-read-in-local-time",
            "title": "Parse date-only strings as UTC everywhere",
            "evidence": {"prs": [{"number": 31, "role": "qa_fail", "quote": "Sep 1 rows land in August"},
                                 {"number": 34, "role": "review_findings", "quote": "ping @dana"}],
                         "files": ["src/reports/monthly.ts:42", "src/budgets/alerts.ts:18"]}}
ARCH_CAND = {"source": "architecture", "kind": "refactor", "fingerprint": "arch:merge-duplicate-date-helpers",
             "title": "Merge the three date helpers into one module",
             "evidence": {"files": ["src/lib/dates.ts:1", "src/utils/time.ts:12"]}}


class AgentCandidateRulesTest(unittest.TestCase):
    def test_valid_candidates_pass(self):
        got = collect.classify([PRS_CAND, ARCH_CAND], [])
        self.assertEqual([g["status"] for g in got], ["new", "new"])

    def test_prs_pattern_must_recur(self):
        one = dict(PRS_CAND, evidence={"prs": [{"number": 31}, {"number": 31}]})
        got = collect.classify([one], [])[0]
        self.assertEqual(got["status"], "invalid")
        self.assertIn("2+ PRs", got["reasons"][0])

    def test_prs_kind_and_fingerprint(self):
        bad = dict(PRS_CAND, kind="ux", fingerprint="prs:Date Bug")
        self.assertEqual(len(collect.problems(bad)), 2)

    def test_architecture_needs_file_line_and_refactor_kind(self):
        bad = dict(ARCH_CAND, kind="bug", evidence={"files": ["src/lib/dates.ts"]})
        self.assertEqual(len(collect.problems(bad)), 2)

    def test_closed_pattern_seen_again_regresses(self):
        cards = [{"number": 5, "state": "CLOSED", "closedAt": "2026-10-01T00:00:00Z",
                  "fingerprint": "prs:date-only-strings-read-in-local-time"}]
        got = collect.classify([dict(PRS_CAND, last_seen="2026-10-20T10:00:00Z")], cards)[0]
        self.assertEqual((got["status"], got["existing"]), ("regressed", 5))


class NewKindRenderTest(unittest.TestCase):
    def test_refactor_criteria_are_checkable(self):
        body = collect.render(dict(ARCH_CAND, verdict="real"))
        self.assertIn("Behaviour is unchanged", body)
        self.assertIn("existing test suite still passes", body)
        self.assertIn("measurable", body)
        self.assertIn("- `src/lib/dates.ts:1`", body)
        self.assertIn("from an architecture review", body)
        self.assertNotIn("events from", body, "no fake counts for sources without them")

    def test_process_card(self):
        body = collect.render(dict(PRS_CAND, kind="process", verdict="real"))
        self.assertIn("written down where the lanes read it", body)
        self.assertIn("PR #31 (qa_fail)", body)
        self.assertNotIn("@dana", body)
        self.assertEqual(collect.FP_RE.search(body).group(1), PRS_CAND["fingerprint"])

    def test_every_kind_has_defaults(self):
        for kind in ("bug", "ux", "tracking", "perf", "process", "refactor", "feature", "task"):
            self.assertIn(kind, collect.DEFAULT_CRITERIA)

    def test_adoption_note(self):
        note = collect.adoption_note({"fingerprint": "github:12", "verdict": "unclear",
                                      "acceptance": ["Export downloads a CSV in Safari 17"]})
        self.assertIn("Adopted by factory collect", note)
        self.assertIn("needs a human look", note)
        self.assertIn("- [ ] Export downloads a CSV in Safari 17", note)
        self.assertIn("factory-fingerprint: `github:12`", note)


class SourcesCliTest(unittest.TestCase):
    def test_defaults(self):
        cfg = {"collect": {"sentry": {"org": "a"}, "posthog": {"enabled": False},
                           "github": {"enabled": True}, "prs": {}, "architecture": {"enabled": True}}}
        self.assertEqual(collect.default_sources(cfg), ["sentry", "github", "architecture"])
        self.assertEqual(collect.default_sources({}), [])


class FilingTest(unittest.TestCase):
    """Runs `file` against tests/fakebin_collect/gh and checks what gh was asked to do."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log = os.path.join(self.tmp, "gh.log")
        cfg = os.path.join(self.tmp, "factory.json")
        with open(cfg, "w") as fh:
            json.dump(CFG, fh)
        self.env = {"FACTORY_CONFIG": cfg, "GH_LOG": self.log,
                    "PATH": os.path.join(HERE, "fakebin_collect") + os.pathsep + os.environ["PATH"]}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def file(self, cards):
        path = os.path.join(self.tmp, "cards.json")
        with open(path, "w") as fh:
            json.dump(cards, fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = collect.main(["file", path])
        log = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                log = fh.read()
        return code, json.loads(out.getvalue()), log

    def test_adopt_keeps_the_original_issue(self):
        card = {"source": "github", "kind": "bug", "fingerprint": "github:12", "title": "Export",
                "number": 12, "url": "https://github.com/acme/app/issues/12", "adopt": True,
                "verdict": "real", "acceptance": ["Export downloads a CSV in Safari 17"]}
        code, out, log = self.file([card])
        self.assertEqual(code, 0)
        self.assertEqual(out["filed"][0], {"number": 12, "url": card["url"], "status": "adopted"})
        self.assertNotIn("gh issue create", log)
        self.assertIn("gh issue edit 12 --repo acme/app --add-label factory:collected", log)
        self.assertIn("--add-label source:github", log)
        self.assertIn("gh project item-add 5 --owner acme --url https://github.com/acme/app/issues/12", log)
        self.assertIn("Adopted by factory collect", log)
        self.assertIn("project item-edit", log, "moved to Backlog")

    def test_refactor_card_is_created(self):
        code, out, log = self.file([dict(ARCH_CAND, verdict="real")])
        self.assertEqual(code, 0)
        self.assertEqual(out["filed"][0]["number"], 99)
        self.assertIn("--label kind:refactor", log)
        self.assertIn("--label source:architecture", log)

    def test_invalid_candidate_is_refused(self):
        one = dict(PRS_CAND, evidence={"prs": [{"number": 31}]})
        code, out, log = self.file([one])
        self.assertEqual(code, collect.EXIT_USAGE)
        self.assertIn(PRS_CAND["fingerprint"], out["invalid"])
        self.assertEqual(log, "")


class SourceCliTest(GHBase):
    def run_cli(self, *argv, cfg=CFG):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
        os.environ["FACTORY_CONFIG"] = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(os.environ.pop, "FACTORY_CONFIG", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = collect.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_github_unavailable_not_empty(self):
        self.gh.fail_on = ("issue list",)
        code, out = self.run_cli("fetch", "github")
        self.assertEqual(code, collect.EXIT_UNAVAILABLE)
        self.assertNotIn("candidates", out)

    def test_prs_fetch_cli(self):
        code, out = self.run_cli("fetch", "prs", "--since", "14d")
        self.assertEqual(code, 0)
        self.assertEqual(out["candidates"], [])
        self.assertEqual(len(out["raw"]["prs"]), 3)

    def test_no_repo_is_disabled(self):
        cfg = {k: v for k, v in CFG.items() if k != "repo"}
        code, out = self.run_cli("fetch", "prs", cfg=cfg)
        self.assertEqual(code, collect.EXIT_DISABLED)

    def test_sources_command(self):
        cfg = dict(CFG, collect=dict(CFG["collect"], prs={"enabled": True}))
        code, out = self.run_cli("sources", cfg=cfg)
        self.assertEqual(out["default"], ["sentry", "posthog", "prs"])


if __name__ == "__main__":
    unittest.main()
