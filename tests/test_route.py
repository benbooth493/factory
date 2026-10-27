"""Tests for scripts/route.py: the grade → model ladder. Run: python3 -m unittest discover -s tests"""

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

import route  # noqa: E402

# (host, tier) -> (router, easy, medium, hard), as the README promises.
EXPECTED = {
    ("claude", "default"): ("haiku", "sonnet", "opus", "inherit"),
    ("claude", "low"): ("haiku", "haiku", "sonnet", "opus"),
    ("claude", "high"): ("sonnet", "opus", "opus", "opus"),
    ("codex", "default"): ("gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-astra"),
    ("codex", "low"): ("gpt-6-luna", "gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-sol"),
    ("codex", "high"): ("gpt-6-luna", "gpt-6-astra", "gpt-6-astra", "gpt-6-astra"),
}


class LadderTest(unittest.TestCase):
    def test_every_flag_grade_host(self):
        for (host, tier), (router, *models) in EXPECTED.items():
            with self.subTest(host=host, tier=tier):
                self.assertEqual(route.router_model({}, tier, host), router)
                for grade, want in zip(route.GRADES, models):
                    for lane in ("build", "qa", "review"):
                        self.assertEqual(route.lane_model({}, lane, grade, tier, host), want,
                                         f"{host}/{tier}/{grade}/{lane}")

    def test_missing_or_unknown_grade_takes_medium(self):
        self.assertEqual(route.lane_model({}, "build", None), "opus")
        self.assertEqual(route.lane_model({}, "build", "bogus", "low"), "sonnet")
        self.assertEqual(route.lane_model({}, "qa", None, "default", "codex"), "gpt-6.1-sol")

    def test_grade_aliases(self):
        self.assertEqual(route.lane_model({}, "build", "LOW"), "sonnet")
        self.assertEqual(route.lane_model({}, "build", "High", "low"), "opus")

    def test_rejects_unknown_tier_and_host(self):
        with self.assertRaises(ValueError):
            route.lane_model({}, "build", "easy", "turbo")
        with self.assertRaises(ValueError):
            route.router_model({}, "default", "opencode")


class ConfigOverrideTest(unittest.TestCase):
    def test_routing_cell_overrides_ladder(self):
        cfg = {"routing": {"claude": {"default": {"hard": "opus", "router": "sonnet"}},
                           "codex": {"low": {"easy": "gpt-x"}}}}
        self.assertEqual(route.lane_model(cfg, "build", "hard"), "opus")
        self.assertEqual(route.lane_model(cfg, "build", "easy"), "sonnet")  # untouched cell
        self.assertEqual(route.router_model(cfg), "sonnet")
        self.assertEqual(route.lane_model(cfg, "build", "easy", "low", "codex"), "gpt-x")

    def test_models_lane_wins_on_claude_only(self):
        cfg = {"models": {"review": "opus", "build": " "},
               "routing": {"claude": {"default": {"easy": "haiku"}}}}
        self.assertEqual(route.lane_model(cfg, "review", "easy"), "opus")
        self.assertEqual(route.lane_model(cfg, "build", "easy"), "haiku")  # blank ignored
        self.assertEqual(route.lane_model(cfg, "review", "easy", "default", "codex"), "gpt-6.1-sol")

    def test_codex_model_config_pins_and_skips_router(self):
        cfg = {"codex": {"model": "gpt-pinned"}, "models": {"build": "sonnet"}}
        for grade in route.GRADES:
            self.assertEqual(route.lane_model(cfg, "build", grade, "high", "codex"), "gpt-pinned")
        self.assertEqual(route.router_model(cfg, "default", "codex"), route.NO_ROUTER)
        self.assertEqual(route.lane_model(cfg, "build", "easy"), "sonnet")  # claude host unaffected
        self.assertEqual(route.router_model(cfg), "haiku")


class PinTest(unittest.TestCase):
    def test_codex_equals_model_pins_everything(self):
        run = route.parse_run_args(["--codex=gpt-5-mini", "--high"], env={})
        self.assertEqual(run, {"host": "codex", "tier": "high", "pin": "gpt-5-mini"})
        cfg = {"codex": {"model": "other"}, "models": {"review": "opus"}}
        for grade in (None,) + route.GRADES:
            for lane in ("build", "qa", "review"):
                self.assertEqual(route.lane_model(cfg, lane, grade, run["tier"], run["host"], run["pin"]),
                                 "gpt-5-mini")
        self.assertEqual(route.router_model(cfg, run["tier"], run["host"], run["pin"]), "none")

    def test_flags(self):
        self.assertEqual(route.parse_run_args([], env={}), {"host": "claude", "tier": "default", "pin": None})
        self.assertEqual(route.parse_run_args(["--once", "--low"], env={})["tier"], "low")
        self.assertEqual(route.parse_run_args(["--codex"], env={})["host"], "codex")
        with self.assertRaises(ValueError):
            route.parse_run_args(["--codex="], env={})
        with self.assertRaises(ValueError):
            route.parse_run_args(["--low", "--high"], env={})

    def test_host_detection(self):
        self.assertEqual(route.detect_host({}), "claude")
        self.assertEqual(route.detect_host({"CODEX_THREAD_ID": "t1", "CLAUDECODE": "1"}), "codex")
        self.assertEqual(route.detect_host({"CODEX_SESSION_ID": "s"}), "codex")
        self.assertEqual(route.detect_host({"FACTORY_HOST": "claude", "CODEX_THREAD_ID": "t"}), "claude")
        self.assertEqual(route.parse_run_args([], env={"CODEX_THREAD_ID": "t"})["host"], "codex")


class CardGradeTest(unittest.TestCase):
    def test_body_line(self):
        for body, want in [
            ("Fix it.\n\nDifficulty: hard\n", "hard"),
            ("**Difficulty:** easy", "easy"),
            ("- difficulty: Medium", "medium"),
            ("Difficulty: low", "easy"),
            ("Difficulty: impossible\nDifficulty: hard", "hard"),
            ("The difficulty here is the cache.", None),
            ("Difficulty: unknown", None),
            ("", None),
        ]:
            with self.subTest(body=body):
                self.assertEqual(route.card_grade({"body": body}), want)

    def test_label_beats_body(self):
        card = {"body": "Difficulty: easy", "labels": [{"name": "bug"}, {"name": "difficulty:hard"}]}
        self.assertEqual(route.card_grade(card), "hard")
        self.assertEqual(route.card_grade({"labels": ["Difficulty:Easy"]}), "easy")

    def test_resolve_order(self):
        live = {"point": "card_lint", "available": True, "act": True, "difficulty": "hard"}
        self.assertEqual(route.resolve({"body": "Difficulty: easy"}, live), {"grade": "easy", "source": "card"})
        self.assertEqual(route.resolve({"body": "no grade"}, live), {"grade": "hard", "source": "jev"})
        shadow = dict(live, act=False)
        self.assertEqual(route.resolve({"body": "x"}, shadow), {"grade": None, "source": None})
        down = dict(live, available=False)
        self.assertEqual(route.resolve({"body": "x"}, down)["grade"], None)
        self.assertEqual(route.resolve("Difficulty: medium"), {"grade": "medium", "source": "card"})


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = os.path.join(self.tmp.name, "factory.json")
        with open(self.cfg, "w") as fh:
            json.dump({"models": {"review": "opus"}}, fh)
        self._env = {k: os.environ.pop(k, None) for k in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "FACTORY_HOST")}
        os.environ["FACTORY_CONFIG"] = self.cfg
        self.addCleanup(self.restore)

    def restore(self):
        os.environ.pop("FACTORY_CONFIG", None)
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = route.main(list(argv))
        return rc, out.getvalue().strip()

    def test_model_and_router(self):
        self.assertEqual(self.run_cli("model", "--lane", "build", "--grade", "hard"), (0, "inherit"))
        self.assertEqual(self.run_cli("model", "--lane", "review", "--grade", "easy"), (0, "opus"))
        self.assertEqual(self.run_cli("model", "--lane", "build", "--grade", "easy", "--tier", "low",
                                      "--host", "codex"), (0, "gpt-6-luna"))
        self.assertEqual(self.run_cli("router", "--tier", "high"), (0, "sonnet"))
        self.assertEqual(self.run_cli("router", "--host", "codex", "--pin", "m"), (0, "none"))
        self.assertEqual(self.run_cli("model", "--lane", "build", "--grade", "nope")[0], 2)

    def test_args_and_card(self):
        rc, out = self.run_cli("args", "--codex=gpt-x", "--low", "--once")
        self.assertEqual((rc, json.loads(out)), (0, {"host": "codex", "tier": "low", "pin": "gpt-x"}))
        self.assertEqual(self.run_cli("args", "--codex=")[0], 2)
        card = os.path.join(self.tmp.name, "card.json")
        with open(card, "w") as fh:
            json.dump({"body": "x", "labels": [{"name": "difficulty:medium"}]}, fh)
        rc, out = self.run_cli("card", card)
        self.assertEqual(json.loads(out), {"grade": "medium", "source": "card"})
        jev = os.path.join(self.tmp.name, "lint.json")
        with open(jev, "w") as fh:
            json.dump({"point": "card_lint", "available": True, "act": True, "difficulty": "easy"}, fh)
        with open(card, "w") as fh:
            fh.write("plain body, no grade")
        rc, out = self.run_cli("card", card, "--jev", jev)
        self.assertEqual(json.loads(out), {"grade": "easy", "source": "jev"})

    def test_missing_config_is_fine(self):
        os.environ["FACTORY_CONFIG"] = os.path.join(self.tmp.name, "absent.json")
        self.assertEqual(self.run_cli("model", "--lane", "review", "--grade", "easy"), (0, "sonnet"))


if __name__ == "__main__":
    unittest.main()
