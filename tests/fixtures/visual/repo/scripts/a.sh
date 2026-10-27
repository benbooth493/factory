#!/usr/bin/env bash
# a.sh — calls b.py.
here=$(cd "$(dirname "$0")" && pwd)
python3 "$here/b.py"
