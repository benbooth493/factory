"""Offline tests for scripts/jev.py: canned AI Gateway responses, no network."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import jev  # noqa: E402


def cfg(**points):
    return {"repo": "acme/app", "jev": {"enabled": True, "points": points or {}}}


def response(answers, cost="0.00001"):
    return {"model": "typesafe-ai/jev", "answers": answers,
            "usage": {"input_tokens": 300, "output_tokens": 20},
            "provider_metadata": {"gateway": {"cost": cost}}}


class FakeGateway:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers, error, []

    def __call__(self, url, key, body, timeout=20):
        self.calls.append((url, key, body))
        if self.error:
            raise jev.JevError(self.error)
        return response(self.answers)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_JEV_LOG"] = os.path.join(self.tmp.name, "log.jsonl")
        os.environ["AI_GATEWAY_API_KEY"] = "vck_test"
        self._http = jev.HTTP
        self.addCleanup(self.cleanup)

    def cleanup(self):
        jev.HTTP = self._http
        os.environ.pop("FACTORY_JEV_LOG", None)
        os.environ.pop("AI_GATEWAY_API_KEY", None)
        self.tmp.cleanup()

    def use(self, answers=None, error=None):
        jev.HTTP = FakeGateway(answers, error)
        return jev.HTTP


class RequestShapeTest(Base):
    def test_request_matches_gateway_contract(self):
        gw = self.use({"sensitive": {"type": "noul", "noul": 0.1}})
        jev.decide(cfg(), "risk_veto", "diff text", "pr-1")
        url, key, body = gw.calls[0]
        self.assertEqual(url, "https://ai-gateway.vercel.sh/typesafe/v1/systemone")
        self.assertEqual(key, "vck_test")
        self.assertEqual(body["model"], "typesafe-ai/jev")
        self.assertEqual(body["state"], "diff text")
        self.assertEqual(body["questions"]["sensitive"]["type"], "noul")

    def test_question_definitions_are_valid(self):
        for point, questions in jev.QUESTIONS.items():
            for qid, q in questions.items():
                self.assertIn(q["type"], ("noul", "score", "choice"), f"{point}.{qid}")
                self.assertTrue(q["instructions"])
                if q["type"] == "score":
                    self.assertIsInstance(q["criteria"], list)
                    self.assertTrue(2 <= len(q["criteria"]) <= 10)
                if q["type"] == "choice":
                    self.assertIsInstance(q["criteria"], dict)
                    self.assertTrue(2 <= len(q["criteria"]) <= 255)

    def test_long_state_truncated(self):
        gw = self.use({"sensitive": {"type": "noul", "noul": 0.1}})
        jev.decide(cfg(), "risk_veto", "x" * 200_000, "pr-2")
        self.assertLessEqual(len(gw.calls[0][2]["state"]), jev.MAX_STATE_CHARS + 20)


class InterpretTest(Base):
    def test_collect_triage_noise(self):
        self.use({"signal": {"type": "choice", "choice": "noise", "confidence": 0.9,
                             "probabilities": {"actionable": 0.03, "noise": 0.94, "expected": 0.03}},
                  "severity": {"type": "score", "score": 0.4}})
        out = jev.decide(cfg(collect_triage="live"), "collect_triage", {"title": "x"}, "sentry:1")
        self.assertEqual(out["label"], "noise")
        self.assertTrue(out["act"])

    def test_collect_triage_uncertain_keeps(self):
        self.use({"signal": {"type": "choice", "choice": "noise",
                             "probabilities": {"actionable": 0.3, "noise": 0.6, "expected": 0.1}},
                  "severity": {"type": "score", "score": 2.1}})
        out = jev.decide(cfg(collect_triage="live"), "collect_triage", {"title": "x"}, "sentry:2")
        self.assertEqual(out["label"], "keep", "70% noise is not enough to skip a verifier")

    def test_card_lint_grade(self):
        self.use({"criteria": {"type": "noul", "noul": 0.88},
                  "difficulty": {"type": "score", "score": 1.8}})
        out = jev.decide(cfg(), "card_lint", "## Acceptance criteria\n- [ ] x", "12")
        self.assertEqual((out["label"], out["difficulty"]), ("pass", "hard"))
        self.assertNotIn("model", out, "route.py owns grade -> model")

    def test_tiers(self):
        self.assertEqual(jev.tier(0.2, 2), "easy")
        self.assertEqual(jev.tier(1.0, 2), "medium")
        self.assertEqual(jev.tier(1.5, 2), "hard")

    def test_bounce_reason(self):
        self.use({"reason": {"type": "choice", "choice": "missing_test", "confidence": 0.7}})
        out = jev.decide(cfg(), "bounce_reason", "QA FAIL: no test for empty cart", "12")
        self.assertEqual(out["label"], "missing_test")

    def test_shadow_never_acts(self):
        self.use({"sensitive": {"type": "noul", "noul": 0.99}})
        out = jev.decide(cfg(), "risk_veto", "diff", "pr-3")
        self.assertEqual((out["mode"], out["label"], out["act"]), ("shadow", "hold", False))


class FailOpenTest(Base):
    def test_disabled(self):
        out = jev.decide({"jev": {"enabled": False}}, "risk_veto", "d", "pr-1")
        self.assertFalse(out["available"])

    def test_point_off(self):
        out = jev.decide(cfg(risk_veto="off"), "risk_veto", "d", "pr-1")
        self.assertEqual(out["reason"], "disabled")

    def test_no_key(self):
        os.environ.pop("AI_GATEWAY_API_KEY")
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                out = jev.decide(cfg(), "risk_veto", "d", "pr-1")
            finally:
                os.chdir(cwd)
        self.assertFalse(out["available"])
        self.assertIn("AI_GATEWAY_API_KEY", out["reason"])

    def test_gateway_error(self):
        self.use(error="HTTP 402: insufficient credit")
        out = jev.decide(cfg(), "risk_veto", "d", "pr-1")
        self.assertFalse(out["available"])
        self.assertIn("402", out["reason"])

    def test_budget_cap(self):
        gw = self.use({"sensitive": {"type": "noul", "noul": 0.1}})
        jev.append_log({"kind": "jev", "point": "risk_veto", "id": "old", "cost": 5.0})
        out = jev.decide(cfg(), "risk_veto", "d", "pr-1")
        self.assertFalse(out["available"])
        self.assertIn("budget", out["reason"])
        self.assertEqual(gw.calls, [], "no call once the budget is spent")


class ReportTest(Base):
    def test_agreement_and_advice(self):
        for i in range(40):
            jev.append_log({"kind": "jev", "point": "bounce_reason", "id": str(i),
                            "label": "logic_bug", "cost": 0.00001})
            jev.append_log({"kind": "claude", "point": "bounce_reason", "id": str(i),
                            "label": "logic_bug" if i < 38 else "missing_test"})
        r = jev.report(jev.read_log())["points"]["bounce_reason"]
        self.assertEqual(r["compared"], 40)
        self.assertEqual(r["agreement"], 0.95)
        self.assertIn("ready for live", r["advice"])

    def test_costly_disagreement_blocks_live(self):
        for i in range(40):
            jev.append_log({"kind": "jev", "point": "collect_triage", "id": str(i), "label": "keep"})
            jev.append_log({"kind": "claude", "point": "collect_triage", "id": str(i), "label": "keep"})
        jev.append_log({"kind": "jev", "point": "collect_triage", "id": "x", "label": "noise"})
        jev.append_log({"kind": "claude", "point": "collect_triage", "id": "x", "label": "keep"})
        r = jev.report(jev.read_log())["points"]["collect_triage"]
        self.assertIn("costly", r["advice"])

    def test_too_few(self):
        r = jev.report([])["points"]["risk_veto"]
        self.assertIsNone(r["agreement"])
        self.assertIn("0/30", r["advice"])

    def test_spend(self):
        jev.append_log({"kind": "jev", "point": "risk_veto", "id": "1", "cost": "0.25"})
        jev.append_log({"kind": "jev", "point": "risk_veto", "id": "2", "cost": 0.5})
        self.assertAlmostEqual(jev.spent_this_month(jev.read_log()), 0.75)


class CliTest(Base):
    def test_record_and_report(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(cfg(), fh)
        os.environ["FACTORY_CONFIG"] = fh.name
        self.addCleanup(os.unlink, fh.name)
        self.addCleanup(os.environ.pop, "FACTORY_CONFIG", None)
        self.assertEqual(jev.main(["record", "risk_veto", "pr-9", "hold"]), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            jev.main(["report"])
        self.assertIn("risk_veto", json.loads(out.getvalue())["points"])


if __name__ == "__main__":
    unittest.main()
