#!/usr/bin/env bash
# env-names.sh — list the variable NAMES in dotenv files, never the values.
# Usage: env-names.sh [file...]   (default: .env .env.local at the repo root)
# The guard hooks block reading .env directly; this is the sanctioned way to
# find out which keys exist (for example SENTRY_AUTH_TOKEN) during onboarding.
set -euo pipefail
root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
[ $# -gt 0 ] || set -- "$root/.env" "$root/.env.local"
for f in "$@"; do
  [ -f "$f" ] || continue
  echo "# $(basename "$f")"
  sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' "$f"
done
