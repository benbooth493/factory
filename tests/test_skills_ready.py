"""Tests for scripts/skills-ready.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "skills-ready.py")
FIXTURE = os.path.join(HERE, "fixtures", "skills")

spec = importlib.util.spec_from_file_location("skills_ready", SCRIPT)
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copytree(FIXTURE, self.tmp, dirs_exist_ok=True)
        self.home = os.path.join(self.tmp, "home")
        self.project = os.path.join(self.tmp, "project")
        manifest = os.path.join(self.home, ".claude", "plugins", "installed_plugins.json")
        with open(manifest) as fh:
            text = fh.read().replace("@HOME@", self.home)
        with open(manifest, "w") as fh:
            fh.write(text)

    def cli(self, *args):
        r = subprocess.run([sys.executable, SCRIPT, "--home", self.home, "--project", self.project, "--json"]
                           + list(args), capture_output=True, text=True)
        return r.returncode, json.loads(r.stdout) if r.stdout else None, r.stderr

    def inv(self):
        return sr.Inventory(self.home, self.project)

    def drop_plugin(self, key):
        path = os.path.join(self.home, ".claude", "plugins", "installed_plugins.json")
        with open(path) as fh:
            data = json.load(fh)
        del data["plugins"][key]
        with open(path, "w") as fh:
            json.dump(data, fh)


class ResolveTest(Base):
    def test_plugin_skill_listed_by_dir(self):
        self.assertTrue(self.inv().resolve("mattpocock-skills:tdd").endswith("engineering/tdd/SKILL.md"))
        self.assertTrue(self.inv().resolve("mattpocock-skills:grilling").endswith("productivity/grilling/SKILL.md"))

    def test_plugin_skills_dir(self):
        self.assertTrue(self.inv().resolve("impeccable:impeccable").endswith("skills/impeccable/SKILL.md"))

    def test_plugin_root_is_the_skill(self):
        self.assertTrue(self.inv().resolve("humanizer:humanizer").endswith("1.0.0/SKILL.md"))

    def test_user_and_project_skills(self):
        self.assertIsNotNone(self.inv().resolve("calibrate"))
        self.assertIsNotNone(self.inv().resolve("our-conventions"))

    def test_empty_skill_does_not_count(self):
        self.assertIsNone(self.inv().resolve("empty"))

    def test_unknown(self):
        self.assertIsNone(self.inv().resolve("mattpocock-skills:nope"))
        self.assertIsNone(self.inv().resolve("nope"))

    def test_bare_install_counts_for_plugin_name(self):
        # `npx skills add` puts tdd in ~/.claude/skills/tdd with no plugin
        self.drop_plugin("mattpocock-skills@mattpocock")
        self.assertIsNone(self.inv().resolve("mattpocock-skills:tdd"))
        os.makedirs(os.path.join(self.home, ".claude", "skills", "tdd"))
        with open(os.path.join(self.home, ".claude", "skills", "tdd", "SKILL.md"), "w") as fh:
            fh.write("---\nname: tdd\n---\nx\n")
        self.assertIsNotNone(self.inv().resolve("mattpocock-skills:tdd"))

    def test_disabled_plugin_does_not_count(self):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as fh:
            json.dump({"enabledPlugins": {"impeccable@impeccable": False}}, fh)
        self.assertIsNone(self.inv().resolve("impeccable:impeccable"))

    def test_project_scoped_plugin_only_for_its_project(self):
        path = os.path.join(self.home, ".claude", "plugins", "installed_plugins.json")
        with open(path) as fh:
            data = json.load(fh)
        data["plugins"]["humanizer@humanizer"][0].update(scope="project", projectPath="/elsewhere")
        with open(path, "w") as fh:
            json.dump(data, fh)
        self.assertIsNone(self.inv().resolve("humanizer:humanizer"))


class RegistryTest(unittest.TestCase):
    def names(self, lane, kind=None):
        return [n for n, _ in sr.required_for(lane, kind)]

    def test_build_by_kind(self):
        self.assertIn("mattpocock-skills:diagnosing-bugs", self.names("build", "bug"))
        self.assertNotIn("mattpocock-skills:implement", self.names("build", "bug"))
        self.assertIn("mattpocock-skills:implement", self.names("build", "feature"))
        self.assertIn("mattpocock-skills:codebase-design", self.names("build", "refactor"))
        self.assertIn("mattpocock-skills:tdd", self.names("build", "refactor"))

    def test_build_without_kind_covers_all_kinds(self):
        names = self.names("build")
        for skill in ("diagnosing-bugs", "implement", "codebase-design", "tdd"):
            self.assertIn("mattpocock-skills:" + skill, names)

    def test_other_lanes(self):
        self.assertEqual(self.names("qa"), ["mattpocock-skills:tdd", "mattpocock-skills:diagnosing-bugs"])
        self.assertIn("mattpocock-skills:code-review", self.names("review"))
        self.assertEqual(self.names("ui-refine-loop"), ["impeccable:impeccable"])
        self.assertEqual(self.names("writing"), ["humanizer:humanizer"])
        self.assertEqual(self.names("collect"), ["mattpocock-skills:improve-codebase-architecture"])


class CliTest(Base):
    def test_all_ready(self):
        code, out, err = self.cli("--all")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["ready"])
        ponytail = [r for r in out["skills"] if r["skill"] == "ponytail:ponytail"][0]
        self.assertEqual(ponytail["status"], "fallback")
        self.assertIn("simplest", ponytail["fallback"])

    def test_missing_pack_exits_10_with_install_steps(self):
        self.drop_plugin("impeccable@impeccable")
        code, out, err = self.cli("--all")
        self.assertEqual(code, 10)
        self.assertFalse(out["ready"])
        self.assertIn("/plugin marketplace add pbakaus/impeccable", err)
        self.assertNotIn("mattpocock", err)

    def test_lane_scope(self):
        self.drop_plugin("impeccable@impeccable")
        code, _, err = self.cli("--lane", "build", "--kind", "bug")
        self.assertEqual(code, 0, err)

    def test_missing_matt_pocock(self):
        self.drop_plugin("mattpocock-skills@mattpocock")
        code, out, err = self.cli("--lane", "qa")
        self.assertEqual(code, 10)
        self.assertEqual({r["status"] for r in out["skills"]}, {"missing"})
        self.assertIn("/plugin marketplace add mattpocock/skills", err)
        self.assertIn("mattpocock-skills@claude-plugins-official", err)

    def test_single_skill(self):
        self.assertEqual(self.cli("--skill", "calibrate")[0], 0)
        self.assertEqual(self.cli("--skill", "calibrate", "--skill", "nope")[0], 10)

    def test_kind_needs_build(self):
        r = subprocess.run([sys.executable, SCRIPT, "--lane", "qa", "--kind", "bug"], capture_output=True)
        self.assertEqual(r.returncode, 2)

    def test_empty_home_is_not_ready(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty)
        r = subprocess.run([sys.executable, SCRIPT, "--home", empty, "--project", empty, "--lane", "review"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 10)
        self.assertIn("not ready", r.stdout)

    def test_read_only(self):
        before = sorted(os.path.join(d, f) for d, _, fs in os.walk(self.tmp) for f in fs)
        self.cli("--all")
        after = sorted(os.path.join(d, f) for d, _, fs in os.walk(self.tmp) for f in fs)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
