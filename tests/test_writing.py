"""Tests for scripts/check-writing.py and docs/writing-standard.md.

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "check-writing.py")
FIX = os.path.join(HERE, "fixtures", "writing")
STANDARD = os.path.join(ROOT, "docs", "writing-standard.md")

spec = importlib.util.spec_from_file_location("check_writing", SCRIPT)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


def messages(kind, text, title=None):
    return [m for _, m in cw.check(kind, text, title)]


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def run(*args, stdin=None):
    return subprocess.run([sys.executable, SCRIPT, *args], input=stdin,
                          capture_output=True, text=True)


class CommitTest(unittest.TestCase):
    def test_good_fixture_passes(self):
        self.assertEqual(messages("commit", fixture("commit_good.txt")), [])

    def test_bad_fixture_reports_each_rule_with_lines(self):
        out = cw.check("commit", fixture("commit_bad.txt"))
        text = "\n".join(f"{n}: {m}" for n, m in out)
        self.assertIn("1: subject is 76 chars", text)
        self.assertIn("1: subject ends with a period", text)
        self.assertIn('1: subject starts with "Added"', text)
        self.assertIn("2: leave a blank line after the subject", text)
        self.assertIn("2: body line is", text)
        self.assertIn('1: banned phrase "comprehensive"', text)
        self.assertIn('2: banned phrase "this commit aims to"', text)
        self.assertIn('2: banned phrase "in order to"', text)
        self.assertNotIn('"aims to"', text)  # covered by the longer phrase

    def test_imperative(self):
        for ok in ["Fix the size check", "Add retries to fetch", "fix(upload): reject big files",
                   "Embed screenshots in the PR", "Bring back the old flag", "Docs: explain gate exit codes",
                   "Need a lock around claims"]:
            self.assertEqual(messages("commit", ok), [], ok)
        for bad in ["Fixed the size check", "Adding retries", "Updates the readme",
                    "feat: added a thing"]:
            self.assertTrue(any("imperative" in m for m in messages("commit", bad)), bad)

    def test_subject_length_boundary(self):
        self.assertEqual(messages("commit", "Fix " + "x" * 68), [])
        self.assertIn("subject is 73 chars; keep it to 72", messages("commit", "Fix " + "x" * 69))

    def test_git_comments_and_trailing_blanks_ignored(self):
        text = "# Please enter the commit message\nFix it\n\n# On branch main\n\n\n"
        self.assertEqual(messages("commit", text), [])

    def test_empty(self):
        self.assertEqual(messages("commit", "\n# only comments\n"), ["commit message is empty"])

    def test_long_urls_and_code_not_wrapped(self):
        body = "Fix it\n\nSee https://example.com/" + "a" * 90 + "\n\n    " + "x " * 50 + "\n"
        self.assertEqual(messages("commit", body), [])


class PrTest(unittest.TestCase):
    def test_good_fixture_passes(self):
        self.assertEqual(messages("pr", fixture("pr_good.md"), "Reject uploads over 10 MB"), [])

    def test_bad_fixture(self):
        out = messages("pr", fixture("pr_bad.md"), "Fixed uploads.")
        self.assertIn('missing "Closes #N" (or "Refs #N" for partial work)', out)
        self.assertIn('missing section "## Why"', out)
        self.assertIn('missing section "## What changed"', out)
        self.assertIn('acceptance criteria need at least one "- [ ]" item', out)
        self.assertIn('banned phrase "this pr aims to"', out)
        self.assertIn("subject ends with a period", out)
        self.assertTrue(any("imperative" in m for m in out))

    def test_refs_and_cross_repo_closes_accepted(self):
        body = fixture("pr_good.md")
        for line in ["Refs #790", "Closes acme/app#790", "fixes #1"]:
            self.assertEqual(messages("pr", body.replace("Closes #790", line)), [], line)

    def test_closes_in_code_does_not_count(self):
        body = fixture("pr_good.md").replace("Closes #790", "`Closes #790`")
        self.assertIn('missing "Closes #N" (or "Refs #N" for partial work)', messages("pr", body))


class CardTest(unittest.TestCase):
    def test_good_fixture_passes(self):
        self.assertEqual(messages("card", fixture("card_good.md"), "Upload spinner never stops over 10 MB"), [])

    def test_bad_fixture(self):
        out = cw.check("card", fixture("card_bad.md"), "#12 " + "x" * 80 + ".")
        msgs = [m for _, m in out]
        self.assertIn('missing section "## Problem"', msgs)
        self.assertIn('missing section "## Acceptance criteria"', msgs)
        self.assertIn((2, 'banned phrase "leverage"'), out)
        self.assertNotIn('banned phrase "in order to"', msgs)  # inside a code fence
        self.assertTrue(any(m.startswith("title is 85 chars") for m in msgs))
        self.assertIn("title ends with a period", msgs)
        self.assertIn("title starts with an issue number; drop it", msgs)

    def test_collect_heading_accepted(self):
        text = fixture("card_good.md").replace("## Problem", "## What is happening")
        self.assertEqual(messages("card", text), [])

    def test_collect_rendered_card_passes(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import collect
        finally:
            sys.path.pop(0)
        body = collect.render({"source": "sentry", "kind": "bug", "fingerprint": "sentry:1",
                               "title": "Checkout crashes on empty cart", "what": "The page crashes.",
                               "count": 12, "users": 3, "acceptance": ["Empty cart renders"]})
        self.assertEqual(messages("card", body), [])


class CommentTest(unittest.TestCase):
    def test_lane_handoff_shapes_pass(self):
        with open(STANDARD, encoding="utf-8") as fh:
            blocks = re.findall(r"```\n(.*?)```", fh.read(), flags=re.S)
        handoffs = [b for b in blocks if b.startswith(("🔨", "🧪", "✅"))]
        self.assertEqual(len(handoffs), 3)
        for b in handoffs:
            self.assertEqual(messages("comment", b), [], b)

    def test_banned_and_empty(self):
        self.assertEqual(messages("comment", "  \n"), ["comment is empty"])
        self.assertEqual(messages("comment", "I have successfully run QA"),
                         ['banned phrase "successfully"'])


class StandardTest(unittest.TestCase):
    """The doc and the checker must agree."""

    def setUp(self):
        with open(STANDARD, encoding="utf-8") as fh:
            self.doc = fh.read()

    def test_banned_list_matches_doc(self):
        section = self.doc.split("Banned phrases", 1)[1].split("##", 1)[0]
        self.assertEqual(re.findall(r"`([^`]+)`", section), cw.BANNED)

    def test_doc_examples_pass(self):
        blocks = re.findall(r"```\n(.*?)```", self.doc, flags=re.S)
        commit = next(b for b in blocks if b.startswith("Reject uploads"))
        pr = next(b for b in blocks if b.startswith("Closes #790"))
        self.assertEqual(messages("commit", commit), [])
        self.assertEqual(messages("pr", pr), [])

    def test_mentions_humanizer_and_sections(self):
        self.assertIn("`humanizer`", self.doc)
        for section in ("## Why", "## What changed", "## Acceptance criteria", "## Problem"):
            self.assertIn(section, self.doc)


class CliTest(unittest.TestCase):
    def test_exit_codes_and_output(self):
        good = run("commit", os.path.join(FIX, "commit_good.txt"))
        self.assertEqual((good.returncode, good.stdout), (0, ""))
        bad_path = os.path.join(FIX, "commit_bad.txt")
        bad = run("commit", bad_path)
        self.assertEqual(bad.returncode, 1)
        self.assertIn(f"{bad_path}:1: subject ends with a period", bad.stdout)

    def test_stdin_and_title(self):
        r = run("pr", "-", "--title", "Added stuff.", stdin=fixture("pr_good.md"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("title: subject ends with a period", r.stdout)
        r = run("card", "-", stdin="no sections\n")
        self.assertIn('<stdin>: missing section "## Problem"', r.stdout)

    def test_usage_errors(self):
        self.assertEqual(run("commit", "/nonexistent/file").returncode, 2)
        self.assertEqual(run("commit", "-", "--title", "x", stdin="Fix it\n").returncode, 2)
        self.assertEqual(run("essay", "-", stdin="").returncode, 2)


if __name__ == "__main__":
    unittest.main()
