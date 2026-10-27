#!/usr/bin/env python3
"""PreToolUse guard for Bash commands.

Blocks, in any part of a compound command:
  * git worktree add outside <project>/.claude/worktrees/
  * rm / find -delete / git clean aimed at the project root itself, or at
    anything outside the project (including ~ and /)
  * git push --force (use --force-with-lease) and pushes to the base branch,
    main or master
  * reading secret files (dotenv, SSH keys, cloud credentials)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (  # noqa: E402
    allow, bash_command, block, cwd, git_subcommand, is_within, load_payload,
    positional, program, project_root, resolve, split_commands,
)

SECRET_PATTERNS = [
    r"(^|/)\.env(\.[\w.-]+)?$",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$",
    r"(^|/)\.ssh/",
    r"(^|/)\.aws/credentials$",
    r"(^|/)\.netrc$",
    r"(^|/)\.npmrc$",
    r"(^|/)\.pypirc$",
    r"(^|/)\.docker/config\.json$",
    r"(^|/)\.kube/config$",
    r"\.(pem|p12|pfx|key)$",
    r"(^|/)credentials\.json$",
    r"(^|/)service-account[\w.-]*\.json$",
    r"(^|/)terraform\.tfvars$",
    r"(^|/)\.sentryclirc$",
]
SAFE_SECRET_SUFFIXES = (".example", ".sample", ".template", ".dist")
READERS = {
    "cat", "less", "more", "head", "tail", "bat", "nl", "strings", "xxd", "od",
    "source", ".", "cp", "scp", "base64", "grep", "rg", "awk", "sed", "jq",
    "openssl", "curl", "tar", "zip",
}
PROTECTED = {"main", "master"}


def config_base(root: str) -> Optional[str]:
    path = os.environ.get("FACTORY_CONFIG") or os.path.join(root, ".claude", "factory.json")
    try:
        with open(path) as fh:
            return json.load(fh).get("base_branch")
    except (OSError, ValueError):
        return None


def is_secret(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.endswith(SAFE_SECRET_SUFFIXES):
        return False
    return any(re.search(pat, p) for pat in SECRET_PATTERNS)


# --- individual checks: return a reason to block, or None ------------------

def check_worktree(words: List[str], root: str, here: str) -> Optional[str]:
    sub, args, git_dir = git_subcommand(words)
    if sub != "worktree" or not args or args[0] != "add":
        return None
    rest, skip = [], False
    for a in args[1:]:
        if skip:
            skip = False
            continue
        if a in ("-b", "-B", "--reason", "--orphan"):
            skip = True
            continue
        if not a.startswith("-"):
            rest.append(a)
    if not rest:
        return None
    base = resolve(git_dir, here) if git_dir else here
    target = resolve(rest[0], base)
    allowed = os.path.join(root, ".claude", "worktrees")
    if not is_within(target, allowed) or os.path.realpath(target) == os.path.realpath(allowed):
        return f"worktrees go under {allowed}/ (got {target}); use scripts/worktree.sh open <issue>"
    return None


def _dangerous_target(target: str, root: str) -> Optional[str]:
    home = os.path.realpath(os.path.expanduser("~"))
    if target in ("/", home):
        return f"refusing to delete {target}"
    if target == root:
        return "refusing to delete the project root"
    if not is_within(target, root):
        return f"refusing to delete {target}: it is outside the project ({root})"
    return None


def check_delete(words: List[str], root: str, here: str) -> Optional[str]:
    prog = program(words)
    if prog in ("rm", "rmdir", "unlink", "shred"):
        for arg in positional(words[1:]):
            reason = _dangerous_target(resolve(arg, here), root)
            if reason:
                return reason
    elif prog == "find" and ("-delete" in words or "-exec" in words and "rm" in words):
        starts = []
        for w in words[1:]:
            if w.startswith("-") or w in ("(", "!"):
                break
            starts.append(w)
        for arg in starts or ["."]:
            reason = _dangerous_target(resolve(arg, here), root)
            if reason:
                return reason
    else:
        sub, args, git_dir = git_subcommand(words)
        if sub == "clean":
            base = resolve(git_dir, here) if git_dir else here
            if not is_within(base, root):
                return f"refusing git clean in {base}: outside the project"
            for arg in positional(args):
                if not is_within(resolve(arg, base), root):
                    return f"refusing git clean on {arg}: outside the project"
    return None


def current_branch(here: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", here, "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def check_push(words: List[str], root: str, here: str) -> Optional[str]:
    sub, args, git_dir = git_subcommand(words)
    if sub != "push":
        return None
    for a in args:
        if a in ("-f", "--force", "--mirror") or (
            a.startswith("-") and not a.startswith("--") and "f" in a[1:]
        ):
            return "force push blocked; use --force-with-lease on your own factory/* branch"
        if a == "--delete" or a == "-d":
            return "deleting remote branches by push is blocked"
    protected = set(PROTECTED)
    base = config_base(root)
    if base:
        protected.add(base)
    refspecs = positional(args)[1:]  # first positional is the remote
    for spec in refspecs:
        if spec.startswith("+"):
            return "force refspec (+) blocked; use --force-with-lease"
        if spec.startswith(":"):
            return "deleting remote branches by push is blocked"
        dest = spec.split(":", 1)[-1].replace("refs/heads/", "")
        if dest in protected or dest == "HEAD" and current_branch(here) in protected:
            return f"direct push to {dest} is blocked; open a PR (the merge gate merges)"
    if not refspecs:
        branch = current_branch(resolve(git_dir, here) if git_dir else here)
        if branch in protected:
            return f"you are on {branch}; direct pushes to it are blocked"
    return None


def check_secrets(words: List[str], root: str, here: str) -> Optional[str]:
    prog = program(words)
    if prog not in READERS:
        return None
    for arg in words[1:]:
        if arg.startswith("-") and "=" not in arg:
            continue
        candidate = arg.split("=", 1)[-1] if arg.startswith("-") else arg
        candidate = candidate.lstrip("<@")
        if candidate and is_secret(candidate):
            return (f"{prog} on {candidate} blocked: it looks like a secrets file. "
                    "To see which variables exist (names only), run the factory scripts/env-names.sh")
    return None


CHECKS = (check_worktree, check_delete, check_push, check_secrets)


def evaluate(command: str, root: str, here: str) -> Optional[str]:
    # Redirections like `< .env` are separate tokens; catch them here too.
    if re.search(r"<\s*\S*\.env(\s|$)", command):
        return "reading a dotenv file via redirection is blocked"
    for words in split_commands(command):
        for check in CHECKS:
            reason = check(words, root, here)
            if reason:
                return reason
    return None


def main() -> None:
    payload = load_payload()
    command = bash_command(payload)
    if not command:
        allow()
    reason = evaluate(command, project_root(payload), cwd(payload))
    if reason:
        block(reason)
    allow()


if __name__ == "__main__":
    main()
