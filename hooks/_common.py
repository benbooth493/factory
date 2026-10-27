"""Shared helpers for factory's PreToolUse guard hooks.

Every guard reads the hook payload from stdin, decides, and either exits 0
(allow) or prints a reason to stderr and exits 2 (block; Claude sees the
reason and can correct course). Python standard library only.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from typing import Iterable, List, Optional

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}


def load_payload() -> dict:
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return {}


def block(reason: str) -> None:
    print(f"factory guard: {reason}", file=sys.stderr)
    sys.exit(2)


def allow() -> None:
    sys.exit(0)


def project_root(payload: dict) -> str:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    return os.path.realpath(root)


def cwd(payload: dict) -> str:
    return os.path.realpath(payload.get("cwd") or project_root(payload))


def bash_command(payload: dict) -> Optional[str]:
    if payload.get("tool_name") != "Bash":
        return None
    return (payload.get("tool_input") or {}).get("command") or None


def split_commands(command: str) -> List[List[str]]:
    """Split a shell line into simple commands, each a list of words.

    Not a full shell parser: good enough to see through `a && b; c | d`.
    If the line can't be tokenised (unbalanced quotes) we fall back to
    whitespace splitting so a guard still gets a look at it.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    commands: List[List[str]] = [[]]
    for tok in tokens:
        if tok in SEPARATORS or set(tok) <= set(";&|()"):
            if commands[-1]:
                commands.append([])
            continue
        commands[-1].append(tok)
    return [strip_prefixes(c) for c in commands if c]


def strip_prefixes(words: List[str]) -> List[str]:
    """Drop env assignments and wrappers like sudo/command/env/nice."""
    wrappers = {"sudo", "command", "env", "nice", "nohup", "time", "exec", "xargs"}
    i = 0
    while i < len(words):
        w = words[i]
        if "=" in w and not w.startswith("-") and w.split("=", 1)[0].isidentifier():
            i += 1
            continue
        if w in wrappers:
            i += 1
            # skip wrapper flags such as `sudo -u root` / `env -i`
            while i < len(words) and words[i].startswith("-"):
                i += 1
            continue
        break
    return words[i:]


def program(words: List[str]) -> str:
    return os.path.basename(words[0]) if words else ""


def resolve(path: str, base: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.realpath(path)


def is_within(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def git_subcommand(words: List[str]) -> tuple:
    """For `git [-C dir] [-c k=v] sub args...` return (sub, args, dir)."""
    if program(words) != "git":
        return ("", [], None)
    i, git_dir = 1, None
    while i < len(words) and words[i].startswith("-"):
        if words[i] in ("-C", "-c", "--git-dir", "--work-tree"):
            if words[i] == "-C" and i + 1 < len(words):
                git_dir = words[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(words):
        return ("", [], git_dir)
    return (words[i], words[i + 1:], git_dir)


def positional(args: Iterable[str]) -> List[str]:
    out, after_ddash = [], False
    for a in args:
        if a == "--" and not after_ddash:
            after_ddash = True
            continue
        if after_ddash or not a.startswith("-"):
            out.append(a)
    return out
