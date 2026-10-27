"""Tests for scripts/readme-sync.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "readme-sync.py")

README = "# pack\n\nIntro.\n\n<!-- skills:start -->\nold table\n<!-- skills:end -->\n\n## After\n"


def skill(root, name, description, display=None):
    os.makedirs(os.path.join(root, "skills", name), exist_ok=True)
    with open(os.path.join(root, "skills", name, "SKILL.md"), "w") as fh:
        fh.write(f"---\nname: {display or name}\ndescription: {description}\n---\n\n# {name}\n")


class ReadmeSyncTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        with open(os.path.join(self.root, "README.md"), "w") as fh:
            fh.write(README)
        skill(self.root, "board", "Factory orchestrator. Drains a board.")
        skill(self.root, "build", "Factory Build lane. Turns a card into a PR.")
        os.makedirs(os.path.join(self.root, ".claude-plugin"))
        with open(os.path.join(self.root, ".claude-plugin", "plugin.json"), "w") as fh:
            fh.write('{"name": "pack"}')

    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, SCRIPT, "--root", self.root] + list(args),
                              capture_output=True, text=True, input=stdin)

    def readme(self):
        with open(os.path.join(self.root, "README.md")) as fh:
            return fh.read()

    def test_check_detects_stale_and_writes_nothing(self):
        r = self.run_cli("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stale", r.stderr)
        self.assertEqual(self.readme(), README)

    def test_rewrites_between_markers_only(self):
        self.assertEqual(self.run_cli().returncode, 0)
        text = self.readme()
        self.assertTrue(text.startswith("# pack\n\nIntro.\n\n<!-- skills:start -->\n"))
        self.assertTrue(text.endswith("<!-- skills:end -->\n\n## After\n"))
        self.assertNotIn("old table", text)
        self.assertIn("| [`board`](skills/board/SKILL.md) | Factory orchestrator. |", text)
        self.assertEqual(self.run_cli("--check").returncode, 0)

    def test_idempotent(self):
        self.run_cli()
        first = self.readme()
        self.run_cli()
        self.assertEqual(self.readme(), first)

    def test_families_group_and_brief(self):
        with open(os.path.join(self.root, "skills", "families.json"), "w") as fh:
            json.dump({"you_type": {"title": "You type", "skills": {"board": "Works the board."}},
                       "board_runs": {"title": "The board runs", "skills": {"build": ""}}}, fh)
        skill(self.root, "stray", "Not in any family.")
        self.run_cli()
        text = self.readme()
        self.assertLess(text.index("**You type**"), text.index("**The board runs**"))
        self.assertIn("| Works the board. |", text)
        self.assertIn("| Factory Build lane. |", text)
        self.assertIn("**Other**", text)
        self.assertIn("skills/stray/SKILL.md", text)

    def test_groups_list_shape(self):
        with open(os.path.join(self.root, "skills", "families.json"), "w") as fh:
            json.dump({"groups": [{"id": "lanes", "title": "The board runs", "skills": {"build": ""}},
                                  {"id": "you", "title": "You type", "skills": {"board": "Works it."}}],
                       "hidden": ["x"]}, fh)
        self.assertEqual(self.run_cli().returncode, 0)
        text = self.readme()
        self.assertLess(text.index("**The board runs**"), text.index("**You type**"))
        self.assertNotIn("**Other**", text)

    def test_folded_description_and_pipes(self):
        os.makedirs(os.path.join(self.root, "skills", "qa"))
        with open(os.path.join(self.root, "skills", "qa", "SKILL.md"), "w") as fh:
            fh.write("---\nname: qa\ndescription: >-\n  Proves a | b criteria.\n  More.\n---\n")
        self.run_cli()
        self.assertIn("| Proves a \\| b criteria. |", self.readme())

    def test_missing_markers(self):
        with open(os.path.join(self.root, "README.md"), "w") as fh:
            fh.write("# no markers\n")
        self.assertEqual(self.run_cli().returncode, 2)

    def test_missing_description(self):
        os.makedirs(os.path.join(self.root, "skills", "bad"))
        with open(os.path.join(self.root, "skills", "bad", "SKILL.md"), "w") as fh:
            fh.write("---\nname: bad\n---\n")
        self.assertEqual(self.run_cli().returncode, 2)

    def hook(self, path):
        payload = json.dumps({"tool_name": "Edit", "cwd": self.root, "tool_input": {"file_path": path}})
        return subprocess.run([sys.executable, SCRIPT, "--hook"], capture_output=True, text=True, input=payload)

    def test_hook_syncs_on_skill_edit(self):
        r = self.hook(os.path.join(self.root, "skills", "build", "SKILL.md"))
        self.assertEqual(r.returncode, 0)
        self.assertIn("skills/build/SKILL.md", self.readme())

    def test_hook_relative_path(self):
        self.hook("skills/board/SKILL.md")
        self.assertNotIn("old table", self.readme())

    def test_hook_ignores_other_files(self):
        self.assertEqual(self.hook(os.path.join(self.root, "README.md")).returncode, 0)
        self.assertEqual(self.hook(os.path.join(self.root, "notes", "SKILL.md")).returncode, 0)
        self.assertEqual(self.readme(), README)

    def test_hook_leaves_non_plugin_repos_alone(self):
        os.remove(os.path.join(self.root, ".claude-plugin", "plugin.json"))
        self.hook(os.path.join(self.root, "skills", "build", "SKILL.md"))
        self.assertEqual(self.readme(), README)

    def test_hook_never_fails(self):
        r = subprocess.run([sys.executable, SCRIPT, "--hook"], capture_output=True, text=True, input="not json")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
