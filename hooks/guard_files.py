#!/usr/bin/env python3
"""PreToolUse guard for Read, Edit and Write.

* Blocks reading or editing secret files (dotenv, SSH keys, credentials).
* Blocks writing a file whose new content contains a live-looking API key.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import allow, block, load_payload  # noqa: E402
from guard_bash import is_secret  # noqa: E402

KEY_PATTERNS = {
    "AWS access key": r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",
    "GitHub token": r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{30,}",
    "OpenAI key": r"\bsk-(proj-)?[A-Za-z0-9]{40,}",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{20,}",
    "Stripe live key": r"\b(sk|rk)_live_[A-Za-z0-9]{20,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN ([A-Z]+ )?PRIVATE KEY-----",
}


def find_key(text: str) -> str | None:
    for name, pattern in KEY_PATTERNS.items():
        if re.search(pattern, text):
            return name
    return None


def evaluate(payload: dict) -> str | None:
    tool = payload.get("tool_name")
    data = payload.get("tool_input") or {}
    path = data.get("file_path") or data.get("path") or ""
    if tool in ("Read", "Edit", "MultiEdit", "Write") and path and is_secret(path):
        return (f"{tool} on {path} blocked: it looks like a secrets file. "
                "Ask the user for the variable names you need instead")
    if tool in ("Write", "Edit", "MultiEdit"):
        chunks = [data.get("content"), data.get("new_string")]
        chunks += [e.get("new_string") for e in data.get("edits") or [] if isinstance(e, dict)]
        for chunk in chunks:
            if chunk:
                found = find_key(chunk)
                if found:
                    return (f"that change writes what looks like a live {found} into {path}; "
                            "read it from the environment instead")
    return None


def main() -> None:
    reason = evaluate(load_payload())
    if reason:
        block(reason)
    allow()


if __name__ == "__main__":
    main()
