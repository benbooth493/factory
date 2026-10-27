"""Offline tests for scripts/skill-map.py and scripts/build-site.py, against a fixture repo."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures", "visual", "repo")


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


skillmap = load("skill_map_t", "skill-map.py")
site = load("build_site_t", "build-site.py")


def fixture_copy(test):
    """Fixture repo + the real site template, in a temp dir."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    root = os.path.join(tmp.name, "repo")
    shutil.copytree(FIX, root)
    shutil.copytree(os.path.join(ROOT, "site"), os.path.join(root, "site"))
    return root


class SkillMapTest(unittest.TestCase):
    def setUp(self):
        self.spec = skillmap.build_spec(FIX)
        d = self.spec["diagrams"][0]
        self.nodes = {n["id"]: n for n in d["nodes"]}
        self.edges = {(e["from"], e["to"]): e.get("label") for e in d["edges"]}

    def test_nodes_from_disk(self):
        self.assertEqual(sorted(self.nodes), sorted([
            "skill:alpha", "skill:beta", "skill:gamma", "script:scripts/a.sh", "script:scripts/b.py",
            "script:scripts/c.py", "hook:hooks.json", "hook:guard.py"]))
        self.assertNotIn("skill:missing", self.nodes)  # in families.json but not on disk
        self.assertNotIn("script:scripts/hidden.py", self.nodes)  # hidden

    def test_groups(self):
        self.assertEqual(self.nodes["skill:alpha"]["label"], "/factory:alpha")
        self.assertEqual(self.nodes["skill:alpha"]["kind"], "skill")
        self.assertEqual(self.nodes["skill:beta"]["kind"], "lane")
        self.assertEqual(self.nodes["skill:gamma"]["kind"], "lane")  # placed by "not for direct use"
        self.assertEqual(self.nodes["skill:alpha"]["detail"].split("\n")[0], "Alpha orchestrates.")
        self.assertIn('Arguments: `run | stop ["why"]`', self.nodes["skill:alpha"]["detail"])

    def test_edges(self):
        self.assertEqual(self.edges[("skill:alpha", "skill:beta")], "runs")
        self.assertIn(("skill:alpha", "script:scripts/a.sh"), self.edges)
        self.assertIn(("skill:beta", "script:scripts/b.py"), self.edges)
        self.assertIn(("script:scripts/a.sh", "script:scripts/b.py"), self.edges)
        self.assertIn(("script:scripts/b.py", "script:scripts/c.py"), self.edges)
        self.assertEqual(self.edges[("hook:hooks.json", "hook:guard.py")], "Bash")

    def test_renders_with_source_links(self):
        h = skillmap.visual.render_page(self.spec, "https://github.com/acme/demo/blob/main/")
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertIn("<title>factory skill map</title>", h)
        self.assertIn("https://github.com/acme/demo/blob/main/skills/alpha/SKILL.md", h)
        self.assertIn("https://github.com/acme/demo/blob/main/scripts/c.py#L1-L2", h)
        self.assertEqual(re.findall(r"<script[^>]*\bsrc=", h), [])

    def test_real_repo_map_covers_every_skill_and_script(self):
        spec = skillmap.build_spec(ROOT)
        ids = {n["id"] for n in spec["diagrams"][0]["nodes"]}
        for d in os.listdir(os.path.join(ROOT, "skills")):
            if os.path.isfile(os.path.join(ROOT, "skills", d, "SKILL.md")):
                self.assertIn("skill:" + d, ids)
        for f in ("board.sh", "worktree.sh", "merge-gate.sh", "collect.py", "jev.py", "env-names.sh", "visual.py"):
            self.assertIn("script:scripts/" + f, ids)
        self.assertNotIn("script:scripts/build-site.py", ids)

    def test_families_json_is_valid(self):
        with open(os.path.join(ROOT, "skills", "families.json")) as fh:
            fam = json.load(fh)
        ids = [g["id"] for g in fam["groups"]]
        self.assertEqual(ids, ["you-type", "board-runs"])
        self.assertEqual(sorted(fam["groups"][1]["skills"]), ["build", "qa", "review"])


class BuildSiteTest(unittest.TestCase):
    def test_build(self):
        root = fixture_copy(self)
        out = os.path.join(root, "_site")
        os.makedirs(out)
        with open(os.path.join(out, "stale.html"), "w") as fh:
            fh.write("old")
        res = site.build(root, out)
        self.assertEqual(res["files"], [".nojekyll", "index.html", os.path.join("skill-map", "index.html")])
        with open(os.path.join(out, "index.html")) as fh:
            h = fh.read()
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertNotIn("{{", h)
        self.assertIn("Cards in, &lt;PRs&gt; out.", h)       # tagline from README, escaped
        self.assertIn("Ready ──► Build ──► Done", h)          # flow block
        self.assertIn("<strong>Build</strong> does &lt;the&gt; work.", h)
        self.assertIn("/plugin install demo", h)              # Install
        self.assertIn("/demo:alpha run", h)                   # Use
        self.assertIn("/factory:alpha", h)                    # skill cards
        self.assertIn("Beta lane.", h)
        self.assertIn("v1.2.3", h)
        self.assertIn('href="https://github.com/acme/demo"', h)
        self.assertIn('href="skill-map/"', h)
        self.assertEqual(re.findall(r"<script[^>]*\bsrc=", h), [])
        with open(os.path.join(out, "skill-map", "index.html")) as fh:
            m = fh.read()
        self.assertIn("https://github.com/acme/demo/blob/", m)

    def test_missing_readme_section_fails(self):
        root = fixture_copy(self)
        p = os.path.join(root, "README.md")
        with open(p) as fh:
            text = fh.read()
        with open(p, "w") as fh:
            fh.write(text.replace("## Install", "## Setup"))
        with self.assertRaises(site.BuildError):
            site.build(root, os.path.join(root, "_site"))

    def test_unknown_slot_fails(self):
        root = fixture_copy(self)
        with open(os.path.join(root, "site", "index.html"), "a") as fh:
            fh.write("{{nope}}")
        with self.assertRaisesRegex(site.BuildError, "nope"):
            site.build(root, os.path.join(root, "_site"))

    def test_real_repo_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = site.build(ROOT, os.path.join(tmp, "_site"))
            self.assertIn("index.html", res["files"])
            self.assertIn(os.path.join("skill-map", "index.html"), res["files"])

    def test_pages_workflow(self):
        with open(os.path.join(ROOT, ".github", "workflows", "pages.yml")) as fh:
            wf = fh.read()
        for needle in ("pages: write", "id-token: write", "actions/upload-pages-artifact",
                       "actions/deploy-pages", "scripts/build-site.py", "branches: [main]"):
            self.assertIn(needle, wf)


if __name__ == "__main__":
    unittest.main()
