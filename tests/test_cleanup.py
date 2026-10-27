"""Tests for hooks/cleanup_worktrees.py: real git worktrees, fake gh. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "cleanup_worktrees.py")
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), check=True, capture_output=True, text=True,
                          env=ENV).stdout.strip()


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(self.root)
        git(self.root, "init", "-q", "-b", "main")
        with open(os.path.join(self.root, "a.txt"), "w") as fh:
            fh.write("a\n")
        git(self.root, "add", "a.txt")
        git(self.root, "commit", "-qm", "base")
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "factory.json"), "w") as fh:
            json.dump({"repo": "acme/app"}, fh)
        # fake gh: PR state per branch from a JSON file
        self.states = os.path.join(self.tmp, "states.json")
        self.gh = os.path.join(self.tmp, "gh")
        with open(self.gh, "w") as fh:
            fh.write("#!/bin/sh\n"
                     f"echo \"$*\" >> '{self.tmp}/gh.log'\n"
                     f"state=$(python3 -c \"import json,sys; print(json.load(open('{self.states}')).get(sys.argv[1], ''))\" \"$3\")\n"
                     "[ -n \"$state\" ] || { echo 'no pull requests found' >&2; exit 1; }\n"
                     "echo \"$state\"\n")
        os.chmod(self.gh, os.stat(self.gh).st_mode | stat.S_IEXEC)
        self.set_states({})

    def set_states(self, states):
        with open(self.states, "w") as fh:
            json.dump(states, fh)

    def worktree(self, n, commit=True):
        path = os.path.join(self.root, ".claude", "worktrees", f"issue-{n}")
        git(self.root, "worktree", "add", "-q", "-b", f"factory/{n}", path)
        if commit:
            with open(os.path.join(path, f"f{n}.txt"), "w") as fh:
                fh.write(f"{n}\n")
            git(path, "add", ".")
            git(path, "commit", "-qm", f"work {n}")
        return path

    def run_hook(self, payload, cwd=None):
        env = dict(ENV, FACTORY_GH=self.gh)
        env.pop("CLAUDE_PROJECT_DIR", None)
        payload.setdefault("cwd", cwd or self.root)
        return subprocess.run([sys.executable, HOOK], input=json.dumps(payload), capture_output=True,
                              text=True, env=env)

    def branches(self):
        return git(self.root, "branch", "--format=%(refname:short)").split()

    def log(self):
        with open(os.path.join(self.root, ".claude", "factory", "cleanup.log")) as fh:
            return fh.read()

    def test_merged_removed_and_branch_deleted_with_recovery_note(self):
        path = self.worktree(1)
        tip = git(path, "rev-parse", "HEAD")
        self.set_states({"factory/1": "MERGED"})
        r = self.run_hook({"hook_event_name": "SessionStart"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn("factory/1", self.branches())
        self.assertIn(f"MERGED\tfactory/1\t{tip}", self.log())
        self.assertIn("removed finished worktrees", r.stdout)
        # the note is enough to bring it back
        git(self.root, "branch", "factory/1", tip)

    def test_closed_removes_worktree_keeps_branch(self):
        path = self.worktree(2)
        self.set_states({"factory/2": "CLOSED"})
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertFalse(os.path.exists(path))
        self.assertIn("factory/2", self.branches())
        self.assertIn("CLOSED\tfactory/2", self.log())

    def test_open_and_no_pr_left_alone(self):
        open_wt, none_wt = self.worktree(3), self.worktree(4)
        self.set_states({"factory/3": "OPEN"})
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertTrue(os.path.exists(open_wt))
        self.assertTrue(os.path.exists(none_wt))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude", "factory", "cleanup.log")))

    def test_dirty_worktree_kept(self):
        path = self.worktree(5)
        with open(os.path.join(path, "wip.txt"), "w") as fh:
            fh.write("unsaved\n")
        self.set_states({"factory/5": "MERGED"})
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertTrue(os.path.exists(os.path.join(path, "wip.txt")))

    def test_locked_and_outside_worktrees_kept(self):
        locked = self.worktree(6)
        git(self.root, "worktree", "lock", locked)
        outside = os.path.join(self.tmp, "elsewhere")
        git(self.root, "worktree", "add", "-q", "-b", "factory/7", outside)
        self.set_states({"factory/6": "MERGED", "factory/7": "MERGED"})
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertTrue(os.path.exists(locked))
        self.assertTrue(os.path.exists(outside))

    def test_worktree_it_runs_in_kept(self):
        path = self.worktree(8)
        self.set_states({"factory/8": "MERGED"})
        self.run_hook({"hook_event_name": "SessionStart"}, cwd=path)
        self.assertTrue(os.path.exists(path))

    def test_post_tool_use_only_after_a_merge(self):
        path = self.worktree(9)
        self.set_states({"factory/9": "MERGED"})
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "gh pr list"}})
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "gh.log")), "no gh calls for unrelated commands")
        self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/merge-gate.sh 12 abc"}})
        self.assertFalse(os.path.exists(path))

    def test_not_a_factory_repo(self):
        os.remove(os.path.join(self.root, ".claude", "factory.json"))
        path = self.worktree(10)
        self.set_states({"factory/10": "MERGED"})
        self.run_hook({"hook_event_name": "SessionStart"})
        self.assertTrue(os.path.exists(path))

    def test_never_fails(self):
        r = subprocess.run([sys.executable, HOOK], input="garbage", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        r = self.run_hook({"hook_event_name": "SessionStart"}, cwd=self.tmp)  # not a git repo
        self.assertEqual(r.returncode, 0)

    def test_dry_run_changes_nothing(self):
        path = self.worktree(11)
        self.set_states({"factory/11": "MERGED"})
        env = dict(ENV, FACTORY_GH=self.gh)
        r = subprocess.run([sys.executable, HOOK, "--dry-run"], cwd=self.root, capture_output=True, text=True, env=env)
        self.assertIn("would remove", r.stdout)
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
