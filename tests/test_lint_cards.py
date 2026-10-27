"""Tests for scripts/lint-cards.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "lint-cards.py")
CARDS = os.path.join(HERE, "fixtures", "lint", "cards.json")

spec = importlib.util.spec_from_file_location("lint_cards", SCRIPT)
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)

with open(CARDS, encoding="utf-8") as fh:
    BY_NUMBER = {c["number"]: c for c in json.load(fh)}


def codes(n, cfg=None):
    return sorted({p["code"] for p in lint.lint_card(BY_NUMBER[n], cfg)})


class RulesTest(unittest.TestCase):
    def test_clear_card_passes(self):
        self.assertEqual(codes(1), [])

    def test_no_criteria_section(self):
        self.assertIn("no-criteria", codes(2))

    def test_weasel_words(self):
        problems = lint.lint_card(BY_NUMBER[3])
        vague = [p["detail"] for p in problems if p["code"] == "vague"]
        self.assertEqual(len(vague), 2, problems)
        self.assertTrue(any("faster" in d for d in vague))
        self.assertTrue(any("etc" in d for d in vague))

    def test_comparative_with_number_is_fine(self):
        details = " ".join(p["detail"] for p in lint.lint_card(BY_NUMBER[3]))
        self.assertNotIn("1500", details)

    def test_bug_without_repro(self):
        self.assertEqual(codes(4), ["no-repro"])

    def test_bug_with_repro_passes(self):
        self.assertEqual(codes(5), [])

    def test_multiple_asks(self):
        self.assertEqual(codes(6), ["multiple-asks"])

    def test_empty_section(self):
        self.assertEqual(codes(7), ["empty-criteria"])

    def test_not_observable(self):
        problems = lint.lint_card(BY_NUMBER[8])
        self.assertEqual([p["code"] for p in problems], ["not-observable"])
        self.assertIn("maintainable", problems[0]["detail"])

    def test_code_fence_ignored(self):
        self.assertEqual(codes(9), ["no-criteria"])

    def test_too_big(self):
        body = "\n".join(f"- [ ] page {i} returns 200" for i in range(10))
        card = {"number": 1, "title": "Add pages", "body": body}
        self.assertEqual([p["code"] for p in lint.lint_card(card)], ["too-big"])
        self.assertEqual(lint.lint_card(card, {"max_criteria": 12}), [])

    def test_extra_vague_words(self):
        card = {"number": 1, "title": "Add x", "body": "- [ ] The page renders in a zippy way"}
        self.assertEqual(lint.lint_card(card), [])
        self.assertEqual([p["code"] for p in lint.lint_card(card, {"extra_vague_words": ["zippy"]})], ["vague"])

    def test_deterministic(self):
        for n in BY_NUMBER:
            self.assertEqual(lint.lint_card(BY_NUMBER[n]), lint.lint_card(BY_NUMBER[n]))


class CliTest(unittest.TestCase):
    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, text=True,
                              env=dict(os.environ, **(env or {})))

    def test_json_output_and_exit(self):
        r = self.run_cli("--cards", CARDS, "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        results = json.loads(r.stdout)
        self.assertEqual(len(results), len(BY_NUMBER))
        self.assertEqual(results[0]["problems"], [])

    def test_table_output(self):
        r = self.run_cli("--cards", CARDS)
        self.assertIn("FLAG #2", r.stdout)
        self.assertIn("ok   #1", r.stdout)
        self.assertIn("need tightening", r.stdout)

    def test_all_clear_exits_zero(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([BY_NUMBER[1]], fh)
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(self.run_cli("--cards", fh.name).returncode, 0)

    def test_reads_board_through_fake_gh(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
        cfg = os.path.join(tmp, "factory.json")
        with open(cfg, "w") as fh:
            json.dump({"owner": "acme", "project_number": 5, "repo": "acme/app"}, fh)
        log = os.path.join(tmp, "gh.log")
        env = {"PATH": os.path.join(HERE, "fakebin") + os.pathsep + os.environ["PATH"],
               "FACTORY_CONFIG": cfg, "GH_LOG": log,
               "FAKE_ISSUE": json.dumps({"number": 3, "title": "Make it better", "body": "pls", "labels": []})}
        r = self.run_cli("--json", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        results = json.loads(r.stdout)
        self.assertEqual(sorted(x["number"] for x in results), [3, 3, 3])  # fake answers every view alike
        with open(log) as fh:
            views = [l for l in fh if l.startswith("gh issue view")]
        self.assertEqual(len(views), 3)  # Ready holds issues 1, 2, 3


if __name__ == "__main__":
    unittest.main()
