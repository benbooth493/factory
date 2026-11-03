"""Tests for workflows/board-wave.js: lane chaining and the result summary.

The script runs under node with stubbed workflow hooks (agent, parallel, log),
so no model is called. Skipped when node is not installed.
Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "workflows", "board-wave.js")

# Wraps the workflow body in an async function with stubbed hooks. Each stubbed
# agent() answers from OUTCOMES["<lane>:#<n>"], a list consumed in order.
HARNESS = r"""
const fs = require('fs')
const [scriptPath, argsJson, outcomesJson] = process.argv.slice(2)
const OUTCOMES = JSON.parse(outcomesJson)
const calls = []
const agent = async (prompt, opts) => {
  calls.push({ label: opts.label, phase: opts.phase, model: opts.model || null,
               claimFirst: prompt.includes('Claim it first') })
  const queue = OUTCOMES[opts.label] || []
  const outcome = queue.shift()
  if (outcome === undefined) return null
  const [lane, n] = opts.label.split(':')
  return { result: `RESULT: ${lane} ${n} ${outcome}`, outcome, released: true }
}
const parallel = async (thunks) => Promise.all(thunks.map((t) => t().catch(() => null)))
const log = () => {}
const body = fs.readFileSync(scriptPath, 'utf8').replace(/^export const meta/m, 'const meta')
const run = new Function('agent', 'parallel', 'log', 'args', `return (async () => {\n${body}\n})()`)
run(agent, parallel, log, JSON.parse(argsJson))
  .then((out) => console.log(JSON.stringify({ out, calls })))
  .catch((e) => { console.error(e.message); process.exit(1) })
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BoardWaveTest(unittest.TestCase):
    def run_wave(self, cards, outcomes):
        args = {"repo": "o/r", "pluginRoot": "/plugin", "cards": cards}
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as f:
            f.write(HARNESS)
        try:
            p = subprocess.run(
                ["node", f.name, SCRIPT, json.dumps(args), json.dumps(outcomes)],
                capture_output=True, text=True, check=False,
            )
        finally:
            os.unlink(f.name)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def card(self, n, lane, models=None):
        return {"number": n, "title": f"card {n}", "lane": lane, "grade": "medium",
                "models": models or {}}

    def test_ready_card_runs_all_three_lanes(self):
        r = self.run_wave([self.card(1, "build")],
                          {"build:#1": ["qa"], "qa:#1": ["review"], "review:#1": ["done"]})
        self.assertEqual([c["label"] for c in r["calls"]], ["build:#1", "qa:#1", "review:#1"])
        self.assertEqual([c["phase"] for c in r["calls"]], ["Build", "QA", "Review"])
        # Only lanes after the first claim the card themselves.
        self.assertEqual([c["claimFirst"] for c in r["calls"]], [False, True, True])
        out = r["out"]["cards"][0]
        self.assertEqual(out["lanes"], "build:qa → qa:review → review:done")
        self.assertEqual(out["outcome"], "done")

    def test_bounce_ends_the_chain(self):
        r = self.run_wave([self.card(2, "build")],
                          {"build:#2": ["qa"], "qa:#2": ["build"]})
        self.assertEqual([c["label"] for c in r["calls"]], ["build:#2", "qa:#2"])
        self.assertEqual(r["out"]["cards"][0]["outcome"], "build")

    def test_review_entry_and_waiting(self):
        r = self.run_wave([self.card(3, "review")], {"review:#3": ["waiting"]})
        self.assertEqual([c["label"] for c in r["calls"]], ["review:#3"])
        self.assertEqual(r["out"]["cards"][0]["outcome"], "waiting")

    def test_dead_lane_is_failed_not_released(self):
        r = self.run_wave([self.card(4, "qa")], {})
        out = r["out"]["cards"][0]
        self.assertEqual(out["outcome"], "failed")
        self.assertFalse(out["released"])

    def test_models_inherit_and_pinned(self):
        r = self.run_wave([self.card(5, "build", {"build": "inherit", "qa": "sonnet"})],
                          {"build:#5": ["qa"], "qa:#5": ["blocked"]})
        self.assertEqual([c["model"] for c in r["calls"]], [None, "sonnet"])

    def test_cards_run_independently(self):
        r = self.run_wave([self.card(6, "build"), self.card(7, "qa")],
                          {"build:#6": ["blocked"], "qa:#7": ["review"], "review:#7": ["done"]})
        outs = {c["number"]: c["outcome"] for c in r["out"]["cards"]}
        self.assertEqual(outs, {6: "blocked", 7: "done"})

    def test_rejects_missing_args(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as f:
            f.write(HARNESS)
        try:
            p = subprocess.run(["node", f.name, SCRIPT, json.dumps({"cards": []}), "{}"],
                               capture_output=True, text=True, check=False)
        finally:
            os.unlink(f.name)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("needs args", p.stderr)


if __name__ == "__main__":
    unittest.main()
