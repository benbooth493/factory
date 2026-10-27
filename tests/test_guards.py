"""Tests for the guard hooks. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "hooks")
sys.path.insert(0, HOOKS)

import guard_bash  # noqa: E402
import guard_files  # noqa: E402


class BashGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "feature", self.root], check=True)
        self.addCleanup(self.tmp.cleanup)

    def verdict(self, command, here=None):
        return guard_bash.evaluate(command, self.root, here or self.root)

    def assertBlocked(self, command, here=None):
        self.assertIsNotNone(self.verdict(command, here), f"expected block: {command}")

    def assertAllowed(self, command, here=None):
        self.assertIsNone(self.verdict(command, here), f"expected allow: {command}")

    # worktrees
    def test_worktree_inside_allowed(self):
        self.assertAllowed("git worktree add -b factory/7 .claude/worktrees/issue-7 origin/main")

    def test_worktree_outside_blocked(self):
        self.assertBlocked("git worktree add ../elsewhere")
        self.assertBlocked("git worktree add -b x /tmp/wt main")
        self.assertBlocked("cd src && git worktree add wt")

    def test_worktree_remove_allowed(self):
        self.assertAllowed("git worktree remove /tmp/anything")

    # deletes
    def test_rm_inside_allowed(self):
        self.assertAllowed("rm -rf build dist node_modules")

    def test_rm_outside_blocked(self):
        for cmd in ("rm -rf /", "rm -rf ~", "rm -rf ..", "rm -rf .", "rm -rf ../other",
                    "sudo rm -rf /etc", "echo hi && rm -rf ~/code", "rm -- -weird ../x"):
            self.assertBlocked(cmd)

    def test_find_delete(self):
        self.assertAllowed("find build -name '*.o' -delete")
        self.assertBlocked("find / -name '*.log' -delete")
        self.assertBlocked("find .. -type f -exec rm {} +")

    def test_git_clean(self):
        self.assertAllowed("git clean -fdx")
        self.assertBlocked("git -C .. clean -fdx")

    # pushes
    def test_push_feature_allowed(self):
        self.assertAllowed("git push -u origin factory/12-fix-login")
        self.assertAllowed("git push --force-with-lease origin factory/12")
        self.assertAllowed("git push")  # on branch 'feature'

    def test_push_force_blocked(self):
        self.assertBlocked("git push -f origin factory/12")
        self.assertBlocked("git push --force origin factory/12")
        self.assertBlocked("git push origin +factory/12")

    def test_push_protected_blocked(self):
        self.assertBlocked("git push origin main")
        self.assertBlocked("git push origin HEAD:master")
        self.assertBlocked("git push origin feature:refs/heads/main")
        self.assertBlocked("git push origin :factory/12")

    def test_bare_push_on_main_blocked(self):
        subprocess.run(["git", "-C", self.root, "checkout", "-q", "-b", "main"], check=True)
        self.assertBlocked("git push")

    def test_config_base_branch_protected(self):
        os.makedirs(os.path.join(self.root, ".claude"))
        with open(os.path.join(self.root, ".claude", "factory.json"), "w") as fh:
            json.dump({"base_branch": "develop"}, fh)
        self.assertBlocked("git push origin develop")

    # secrets
    def test_secret_reads_blocked(self):
        for cmd in ("cat .env", "less config/.env.production", "source .env.local",
                    "cat ~/.ssh/id_ed25519", "head ~/.aws/credentials", "cp .env /tmp/x",
                    "grep KEY .env", "cat < .env"):
            self.assertBlocked(cmd)

    def test_env_names_helper_allowed(self):
        self.assertAllowed("bash scripts/env-names.sh")
        self.assertAllowed("/plugins/factory/scripts/env-names.sh .env")
        self.assertAllowed("python3 scripts/collect.py fetch sentry --since 14d")
        self.assertBlocked("cat .sentryclirc")

    def test_safe_reads_allowed(self):
        for cmd in ("cat .env.example", "cat README.md", "ls -la", "grep -r TODO src",
                    "echo $HOME", "python3 -m pytest"):
            self.assertAllowed(cmd)


class FileGuardTest(unittest.TestCase):
    def test_read_secret_blocked(self):
        self.assertIsNotNone(guard_files.evaluate(
            {"tool_name": "Read", "tool_input": {"file_path": "/repo/.env"}}))
        self.assertIsNone(guard_files.evaluate(
            {"tool_name": "Read", "tool_input": {"file_path": "/repo/.env.example"}}))

    def test_write_live_key_blocked(self):
        fake = "AKIA" + "ABCDEFGHIJKLMNOP"
        self.assertIsNotNone(guard_files.evaluate({
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/app.py", "content": f"KEY = '{fake}'"},
        }))
        gh = "ghp_" + "a" * 36
        self.assertIsNotNone(guard_files.evaluate({
            "tool_name": "Edit",
            "tool_input": {"file_path": "/repo/x.ts", "old_string": "a", "new_string": gh},
        }))

    def test_plain_write_allowed(self):
        self.assertIsNone(guard_files.evaluate({
            "tool_name": "Write",
            "tool_input": {"file_path": "/repo/app.py", "content": "token = os.environ['TOKEN']"},
        }))


class HookProcessTest(unittest.TestCase):
    """End to end: the hook as Claude Code runs it (stdin JSON, exit code)."""

    def run_hook(self, script, payload):
        return subprocess.run(
            [sys.executable, os.path.join(HOOKS, script)],
            input=json.dumps(payload), capture_output=True, text=True,
        )

    def test_block_exit_code(self):
        r = self.run_hook("guard_bash.py", {
            "tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": "rm -rf /"}})
        self.assertEqual(r.returncode, 2)
        self.assertIn("factory guard", r.stderr)

    def test_allow_exit_code(self):
        r = self.run_hook("guard_bash.py", {
            "tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": "ls"}})
        self.assertEqual(r.returncode, 0)

    def test_garbage_input_allows(self):
        r = subprocess.run([sys.executable, os.path.join(HOOKS, "guard_files.py")],
                           input="not json", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
