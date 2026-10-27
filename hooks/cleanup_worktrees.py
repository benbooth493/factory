#!/usr/bin/env python3
"""cleanup_worktrees.py — remove lane worktrees once their PR is merged or closed.

Each card gets a worktree under .claude/worktrees/. Once its PR is merged or
closed, the worktree is dead weight, so this removes it:

  - SessionStart: sweep every worktree under .claude/worktrees/
  - PostToolUse (Bash): the same, after a command that merges a PR
    (`gh pr merge …` or factory's merge-gate.sh)
  - by hand: `python3 hooks/cleanup_worktrees.py --dry-run` prints the plan

What it removes, and what it never touches:
  PR MERGED  worktree removed, local branch deleted (the work is on the base)
  PR CLOSED  worktree removed, local branch KEPT (the work isn't anywhere else)
  no PR, open PR, uncommitted changes, locked, detached, or the worktree this
  runs from: left alone

Before removing anything it appends the branch tip to
.claude/factory/cleanup.log, so a branch can always be brought back:
  git branch <branch> <sha>

Only runs in a repo with .claude/factory.json. A hook never fails the tool
call it follows: every error is swallowed and it exits 0.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

MERGE_WORDS = ("gh pr merge", "merge-gate.sh")


def git(cwd: str, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def main_root(cwd: str) -> Optional[str]:
    """The main checkout, even when called from inside a worktree."""
    listing = git(cwd, "worktree", "list", "--porcelain")
    if not listing:
        return None
    first = listing.splitlines()[0]
    return os.path.realpath(first.split(" ", 1)[1]) if first.startswith("worktree ") else None


def worktrees(root: str) -> List[Dict[str, object]]:
    out, cur = [], {}  # type: List[Dict[str, object]], Dict[str, object]
    for line in (git(root, "worktree", "list", "--porcelain") or "").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur["path"] = os.path.realpath(value)
        elif key == "HEAD":
            cur["sha"] = value
        elif key == "branch":
            cur["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key in ("locked", "detached"):
            cur[key] = True
    return out


def pr_state(root: str, repo: Optional[str], branch: str) -> Optional[str]:
    cmd = [os.environ.get("FACTORY_GH", "gh"), "pr", "view", branch, "--json", "state", "--jq", ".state"]
    if repo:
        cmd[4:4] = ["--repo", repo]
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=20,
                           env=dict(os.environ, GH_PROMPT_DISABLED="1", GH_PAGER="cat"))
        return r.stdout.strip().upper() or None if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def plan(root: str, here: str, repo: Optional[str]) -> List[Dict[str, object]]:
    base = os.path.join(root, ".claude", "worktrees") + os.sep
    actions = []
    for wt in worktrees(root):
        path = str(wt.get("path", ""))
        if not path.startswith(base) or wt.get("locked") or wt.get("detached") or not wt.get("branch"):
            continue
        if here == path or here.startswith(path + os.sep):
            continue
        status = git(path, "status", "--porcelain")
        if status is None or status:
            continue  # uncommitted work, or unreadable: never remove
        state = pr_state(root, repo, str(wt["branch"]))
        if state in ("MERGED", "CLOSED"):
            actions.append(dict(wt, state=state, delete_branch=(state == "MERGED")))
    return actions


def record(root: str, actions: List[Dict[str, object]]) -> str:
    log_dir = os.path.join(root, ".claude", "factory")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "cleanup.log")
    new = not os.path.exists(path)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("# factory worktree cleanup. Restore a branch with: git branch <branch> <sha>\n"
                     "# when\tPR state\tbranch\tsha\tworktree\tbranch deleted\n")
        for a in actions:
            fh.write(f"{now}\t{a['state']}\t{a['branch']}\t{a['sha']}\t{a['path']}\t"
                     f"{'yes' if a['delete_branch'] else 'no'}\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def apply(root: str, actions: List[Dict[str, object]]) -> List[str]:
    done = []
    for a in actions:
        if git(root, "worktree", "remove", str(a["path"])) is None:
            continue
        if a["delete_branch"]:
            git(root, "branch", "-D", str(a["branch"]))
        done.append(f"{a['branch']} ({str(a['state']).lower()})")
    git(root, "worktree", "prune")
    return done


def sweep(cwd: str, dry_run: bool = False) -> List[str]:
    root = main_root(cwd)
    if not root or not os.path.isfile(os.path.join(root, ".claude", "factory.json")):
        return []
    try:
        with open(os.path.join(root, ".claude", "factory.json"), encoding="utf-8") as fh:
            repo = json.load(fh).get("repo")
    except (OSError, ValueError, AttributeError):
        repo = None
    actions = plan(root, os.path.realpath(cwd), repo)
    if not actions:
        return []
    if dry_run:
        return [f"would remove {a['path']} ({a['branch']} at {a['sha']}, PR {a['state']})" for a in actions]
    record(root, actions)  # the recovery note goes down before anything is removed
    return apply(root, actions)


def should_run(payload: dict) -> bool:
    event = payload.get("hook_event_name")
    if event == "SessionStart" or not event:
        return True
    if event == "PostToolUse" and payload.get("tool_name") == "Bash":
        command = (payload.get("tool_input") or {}).get("command") or ""
        return any(w in command for w in MERGE_WORDS)
    return False


def main(argv: List[str]) -> int:
    if "--dry-run" in argv:
        for line in sweep(os.getcwd(), dry_run=True) or ["nothing to clean up"]:
            print(line)
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or not should_run(payload):
            return 0
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        removed = sweep(cwd)
        if removed:
            print("factory: removed finished worktrees: " + ", ".join(removed)
                  + " (tips saved in .claude/factory/cleanup.log)")
    except Exception:  # a hook never fails the call it follows
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
