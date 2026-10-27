"""Offline tests for scripts/collect.py. Replays canned Sentry and PostHog payloads."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
