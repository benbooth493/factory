"""Offline tests for scripts/visual.py (renderer, source links, scan, recap facts)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import visual  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "visual", "repo")
BASE = "https://github.com/acme/demo/blob/main/"
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>'


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names, self.attrs, self.hrefs = [], [], []

    def handle_starttag(self, tag, attrs):
        self.names.append(tag)
        self.attrs += [k for k, _ in attrs]
        self.hrefs += [v or "" for k, v in attrs if k == "href"]


def page(spec, base=None):
    return visual.render_page(spec, base)


def scripts_srcs(html):
    return re.findall(r"<script[^>]*\bsrc\s*=", html, re.I)


class RenderTest(unittest.TestCase):
    SPEC = {
        "kind": "recap", "title": "Add the thing", "subtitle": "Why it matters",
        "summary": "Adds `x`.\n\n- one\n- two", "meta": {"branch": "feat/x"},
        "highlights": [{"title": "New lane", "detail": "**bold** move"}],
        "diagrams": [{"title": "Modules", "nodes": [
            {"id": "a", "label": "A", "kind": "skill", "change": "added", "source": "scripts/a.sh:12-20"},
            {"id": "b", "label": "B", "sub": "helper", "detail": "does b", "source": ["scripts/b.py:3", "README.md"]},
        ], "edges": [{"from": "a", "to": "b", "label": "calls"}, {"from": "a", "to": "zz"}]}],
        "commits": [{"sha": "abc1234", "subject": "feat: x", "author": "Ben"}],
        "files": [{"path": "scripts/a.sh", "status": "A", "added": 10, "removed": 0, "note": "new"},
                  {"path": "README.md", "status": "M", "added": 2, "removed": 1}],
        "risks": [{"level": "high", "text": "auth"}],
        "tests": [{"name": "make test", "cmd": "make test", "status": "pass"}],
    }

    def test_document_shape(self):
        h = page(self.SPEC)
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertIn("<title>Add the thing</title>", h)
        self.assertIn('name="viewport"', h)
        self.assertIn("prefers-color-scheme:dark", h)
        self.assertEqual(scripts_srcs(h), [], "no external scripts")
        self.assertNotRegex(h, r'<link[^>]+href="https?:')
        self.assertNotRegex(h, r"@import|url\(https?:")
        for heading in ("Summary", "Diagram", "Highlights", "Commits (1)", "Files", "Risks", "Tests"):
            self.assertIn(">%s</h2>" % heading, h)
        self.assertIn("<code>x</code>", h)
        self.assertIn("<strong>bold</strong>", h)

    def test_deterministic(self):
        self.assertEqual(page(self.SPEC, BASE), page(self.SPEC, BASE))

    def test_dangling_edges_and_badges(self):
        h = page(self.SPEC)
        self.assertEqual(h.count('class="edge'), 1)  # a->zz dropped
        self.assertIn(">NEW</text>", h)
        self.assertIn('marker-end="url(#ah0)"', h)

    def test_requires_title(self):
        with self.assertRaises(SystemExit):
            page({"kind": "recap"})

    def test_xss_in_title_and_paths_is_escaped(self):
        spec = {"kind": "map", "title": XSS, "subtitle": XSS, "summary": XSS, "meta": {XSS: XSS},
                "highlights": [{"title": XSS, "detail": XSS}],
                "diagrams": [{"title": XSS, "caption": XSS, "columns": [XSS], "nodes": [
                    {"id": XSS, "label": XSS, "sub": XSS, "kind": XSS, "change": XSS, "detail": XSS,
                     "source": XSS + ":1-2"}], "edges": [{"from": XSS, "to": XSS, "label": XSS}]}],
                "files": [{"path": XSS, "status": XSS, "note": XSS}],
                "commits": [{"sha": XSS, "subject": XSS, "author": XSS}],
                "risks": [{"level": XSS, "text": XSS}], "tests": [{"name": XSS, "cmd": XSS, "status": XSS}],
                "steps": [{"title": XSS, "detail": XSS, "files": [XSS]}],
                "questions": [{"q": XSS, "default": XSS}], "sections": [{"title": XSS, "body": XSS}]}
        h = page(spec, BASE)
        self.assertNotIn("<script>alert", h)
        self.assertNotIn("<img", h)
        tags = Tags()
        tags.feed(h)
        self.assertEqual(tags.names.count("script"), 2)  # our data block + our code
        self.assertNotIn("img", tags.names)
        self.assertEqual([a for a in tags.attrs if a.startswith("on")], [])
        self.assertFalse(any(v.lower().startswith("javascript:") for v in tags.hrefs))
        self.assertIn("<title>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", h)
        data = re.search(r'<script type="application/json" id="factory-data">(.*?)</script>', h, re.S).group(1)
        self.assertNotIn("<", data)
        self.assertEqual(json.loads(data)["0:" + XSS]["label"], XSS)  # round-trips intact

    def test_markdown_links_only_http_or_relative(self):
        h = visual.inline_md("[a](javascript:alert(1)) [b](https://ok.example/x) [c](docs/x.md)")
        self.assertNotIn('href="javascript', h)
        self.assertIn('href="https://ok.example/x"', h)
        self.assertIn('href="docs/x.md"', h)

    def test_markdown_blocks(self):
        h = visual.md("### Head\n\n```\n<b>\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n1. x\n2. y")
        self.assertIn("<h4>Head</h4>", h)
        self.assertIn("<pre><code>&lt;b&gt;</code></pre>", h)
        self.assertIn("<td>2</td>", h)
        self.assertIn("<ol><li>x</li><li>y</li></ol>", h)

    def test_plan_steps_become_flow(self):
        h = page({"kind": "plan", "title": "P", "steps": [{"title": "One", "files": ["a.py"]}, {"title": "Two"}]})
        self.assertIn(">Diagram</h2>", h)
        self.assertIn('data-id="step-2"', h)
        self.assertEqual(h.count('class="edge'), 1)
        self.assertIn(">Steps</h2>", h)

    def test_layout_top_align_and_loose_nodes(self):
        nodes = [{"id": str(i), "label": str(i)} for i in range(10)]
        L = visual.layout({"nodes": nodes, "edges": [{"from": "0", "to": "1"}]})
        self.assertGreater(len(L["keys"]), 2, "loose nodes spread over columns")
        L = visual.layout({"align": "top", "nodes": [{"id": "a", "col": 0}, {"id": "b", "col": 1}, {"id": "c", "col": 1}],
                           "edges": []})
        self.assertEqual(L["place"]["a"][1], L["place"]["b"][1])

    def test_cycles_do_not_hang(self):
        L = visual.layout({"nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                           "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"}]})
        self.assertEqual(sorted(L["place"]), ["a", "b", "c"])


class SourceLinkTest(unittest.TestCase):
    def test_line_ranges(self):
        self.assertEqual(visual.source_link("scripts/a.sh:12-20", BASE),
                         ("scripts/a.sh:12-20", BASE + "scripts/a.sh#L12-L20"))
        self.assertEqual(visual.source_link("scripts/a.sh:12", BASE)[1], BASE + "scripts/a.sh#L12")
        self.assertEqual(visual.source_link("scripts/a.sh", BASE)[1], BASE + "scripts/a.sh")
        self.assertEqual(visual.source_link("./x/y z.py:3", BASE)[1], BASE + "x/y%20z.py#L3")

    def test_no_base_or_unsafe_path_is_text(self):
        self.assertIsNone(visual.source_link("scripts/a.sh:1", None)[1])
        self.assertIsNone(visual.source_link("/etc/passwd", BASE)[1])
        self.assertIsNone(visual.source_link("../up.py", BASE)[1])
        self.assertIsNone(visual.source_link("~/x", BASE)[1])

    def test_base_normalised_and_validated(self):
        self.assertEqual(visual.check_base("https://x.example/b"), "https://x.example/b/")
        for bad in ("javascript:alert(1)", 'https://x/"onmouseover=1', "ftp://x/"):
            with self.assertRaises(SystemExit):
                visual.check_base(bad)

    def test_rendered_links(self):
        h = page(RenderTest.SPEC, BASE)
        self.assertIn('href="%sscripts/a.sh"' % BASE, h)  # files table
        self.assertIn('href="https://github.com/acme/demo/commit/abc1234"', h)
        data = json.loads(re.search(r'id="factory-data">(.*?)</script>', h, re.S).group(1))
        self.assertEqual(data["0:a"]["sources"][0]["href"], BASE + "scripts/a.sh#L12-L20")
        self.assertEqual([s["href"] for s in data["0:b"]["sources"]], [BASE + "scripts/b.py#L3", BASE + "README.md"])
        self.assertEqual(data["0:a"]["out"], [["b", "calls"]])
        self.assertEqual(data["0:b"]["names"], {"a": "A"})

    def test_without_base_no_source_links(self):
        h = page(RenderTest.SPEC)
        self.assertNotIn("github.com", h)


class ScanTest(unittest.TestCase):
    def test_scan_scripts(self):
        spec = visual.scan(os.path.join(FIX, "scripts"), FIX)
        d = spec["diagrams"][0]
        ids = {n["id"] for n in d["nodes"]}
        self.assertEqual(ids, {"scripts/a.sh", "scripts/b.py", "scripts/c.py", "scripts/hidden.py"})
        pairs = {(e["from"], e["to"]) for e in d["edges"]}
        self.assertIn(("scripts/a.sh", "scripts/b.py"), pairs)   # shell calls $here/b.py
        self.assertIn(("scripts/b.py", "scripts/c.py"), pairs)   # python import
        self.assertNotIn(("scripts/b.py", "scripts/a.sh"), pairs)  # mention in a docstring is not a call
        b = next(n for n in d["nodes"] if n["id"] == "scripts/b.py")
        self.assertEqual(b["detail"], "helper used by a.sh. Mentions c.py in prose only.")
        self.assertEqual(b["source"], "scripts/b.py:1-3")

    def test_scan_ts_relative_imports(self):
        spec = visual.scan(os.path.join(FIX, "pkg"), FIX)
        pairs = {(e["from"], e["to"]) for e in spec["diagrams"][0]["edges"]}
        self.assertEqual(pairs, {("pkg/main.ts", "pkg/sub/util.ts")})

    def test_scan_groups_when_large(self):
        spec = visual.scan(os.path.join(FIX, "pkg"), FIX, max_nodes=1)
        ids = sorted(n["id"] for n in spec["diagrams"][0]["nodes"])
        self.assertEqual(ids, [".", "sub"])
        self.assertEqual([(e["from"], e["to"]) for e in spec["diagrams"][0]["edges"]], [(".", "sub")])


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T",
                            GIT_COMMITTER_EMAIL="t@e"))


class FactsTest(unittest.TestCase):
    def test_recap_facts_and_cli_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            git(root, "init", "-q", "-b", "main")
            os.makedirs(os.path.join(root, "scripts"))
            os.makedirs(os.path.join(root, "hooks"))
            with open(os.path.join(root, "scripts", "c.py"), "w") as fh:
                fh.write("x = 1\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            git(root, "checkout", "-qb", "feat/auth")
            with open(os.path.join(root, "hooks", "auth_guard.py"), "w") as fh:
                fh.write("import sys\nsys.path.insert(0, 'scripts')\nfrom scripts import c\n")
            with open(os.path.join(root, "scripts", "c.py"), "a") as fh:
                fh.write("y = 2\n")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "feat: <guard>")
            with open(os.path.join(root, "new.txt"), "w") as fh:
                fh.write("a\nb\n")
            os.makedirs(os.path.join(root, ".claude"))
            with open(os.path.join(root, ".claude", "factory.json"), "w") as fh:
                json.dump({"base_branch": "main", "verify_commands": ["make test"]}, fh)
            spec = visual.recap_facts(root)
            self.assertEqual(spec["meta"]["branch"], "feat/auth")
            self.assertEqual(spec["meta"]["base"], "main")
            self.assertEqual([c["subject"] for c in spec["commits"]], ["feat: <guard>"])
            files = {f["path"]: f for f in spec["files"]}
            self.assertEqual(files["hooks/auth_guard.py"]["status"], "A")
            self.assertEqual(files["scripts/c.py"]["added"], 1)
            self.assertEqual(files["new.txt"]["note"], "untracked")
            nodes = {n["id"]: n for n in spec["diagrams"][0]["nodes"]}
            self.assertEqual(nodes["hooks"]["change"], "added")
            self.assertEqual(nodes["scripts"]["change"], "modified")
            self.assertTrue(any("auth" in r["text"] for r in spec["risks"]))
            self.assertTrue(any("no test files" in r["text"] for r in spec["risks"]))
            self.assertEqual(spec["tests"], [{"name": "make test", "cmd": "make test", "status": "unknown"}])
            # CLI render to the default location
            path = os.path.join(root, "spec.json")
            with open(path, "w") as fh:
                json.dump(spec, fh)
            cwd = os.getcwd()
            os.chdir(root)
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(visual.main(["render", path]), 0)
            finally:
                os.chdir(cwd)
            out = json.loads(buf.getvalue())["out"]
            self.assertEqual(out, os.path.join(root, ".claude", "factory", "visual", "recap-feat-auth.html"))
            with open(out) as fh:
                h = fh.read()
            self.assertIn("feat: &lt;guard&gt;", h)


if __name__ == "__main__":
    unittest.main()
