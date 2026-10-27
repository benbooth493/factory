#!/usr/bin/env bash
# codex-lane.sh — run one factory lane on OpenAI Codex instead of a Claude subagent.
#
# Usage: codex-lane.sh <build|qa|review> <issue> [--model M] [--timeout SECONDS]
#
# Starts `codex exec` non-interactively at the repository root (the card's
# worktree lives under .claude/worktrees/ and the lane skill opens it), with a
# prompt telling Codex to read the lane's SKILL.md and follow it. Codex's
# output goes to .claude/factory/codex/<lane>-<issue>.log. The lane's final
# `RESULT: <lane> #<issue> ...` line is printed on stdout.
#
# Exit: 0 RESULT found · 3 no RESULT line · 124 timed out · 127 codex missing
#       64 bad usage.
#
# Config (.claude/factory.json, all optional):
#   codex.model            model when --model is not given (else Codex's default)
#   codex.timeout_minutes  wall-clock limit per lane (default 60)
#
# Sandbox: a named permission profile, read-only everywhere except the repo
# (worktrees and .git included) and temp dirs, with network on for gh and
# git push. The Codex flags follow super-board's invocation recorded against
# codex-cli 0.160.0; factory has not run them against a real CLI (docs/hosts.md).

set -euo pipefail

die() { echo "codex-lane: $*" >&2; exit "${2:-64}"; }

[ $# -ge 2 ] || die "usage: codex-lane.sh <build|qa|review> <issue> [--model M] [--timeout SECONDS]"
lane="$1"; issue="$2"; shift 2
case "$lane" in build|qa|review) ;; *) die "unknown lane: $lane" ;; esac
case "$issue" in ''|*[!0-9]*) die "issue must be a number: $issue" ;; esac

model=""; timeout=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) [ $# -ge 2 ] || die "--model needs a value"; model="$2"; shift 2 ;;
    --model=*) model="${1#--model=}"; shift ;;
    --timeout) [ $# -ge 2 ] || die "--timeout needs a value"; timeout="$2"; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done

command -v codex >/dev/null 2>&1 || die "codex CLI not found on PATH (npm i -g @openai/codex, then codex login)" 127

plugin_root="$(cd "$(dirname "$0")/.." && pwd)"
# The main checkout, even when called from inside a card worktree.
common="$(git rev-parse --git-common-dir 2>/dev/null)" || die "run inside the target git repository"
root="$(cd "$common/.." && pwd)"
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"

cfg() { # cfg <jq-path> <default>
  local v=""
  [ -f "$config" ] && v=$(jq -r "$1 // empty" "$config" 2>/dev/null || true)
  if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "$2"; fi
}

[ -n "$model" ] || model=$(cfg .codex.model "")
[ "$model" = "inherit" ] && model=""   # a Claude ladder value means nothing to Codex
if [ -z "$timeout" ]; then
  timeout=$(( $(cfg .codex.timeout_minutes 60) * 60 ))
fi
case "$timeout" in ''|*[!0-9]*|0) die "timeout must be a positive number of seconds: $timeout" ;; esac
git_dir="$(cd "$common" && pwd)"

logdir="$root/.claude/factory/codex"
mkdir -p "$logdir"
log="$logdir/$lane-$issue.log"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
last="$tmp/last-message.txt"

cat > "$tmp/prompt.md" <<EOF
You are the factory $lane lane for issue #$issue, running in OpenAI Codex.

Read $plugin_root/skills/$lane/SKILL.md and follow it exactly. It refers to
\${CLAUDE_PLUGIN_ROOT}; that is $plugin_root (already exported in your shell).
The repository root is $root and the config is $config. Run factory scripts
from the repository root; do the card's work in its worktree under
$root/.claude/worktrees/ as the skill says.

Codex differences, nothing else changes:
- Do not start sub-agents; do every step yourself, in order.
- The guard hooks do not run here, so keep their rules yourself: never push
  to the base branch or force-push, never merge except through merge-gate.sh,
  never read .env files or credentials, never delete outside the worktree.

Finish by printing the single final RESULT line the skill asks for, starting
with "RESULT: $lane #$issue", as the very last line of your reply.
EOF

fs="{\":root\"=\"read\", \":slash_tmp\"=\"write\", \":tmpdir\"=\"write\", \"$root\"=\"write\", \"$git_dir\"=\"write\"}"
args=(exec -C "$root" -c 'approval_policy="never"'
  -c 'default_permissions="factory-lane"'
  -c "permissions.factory-lane.filesystem=$fs"
  -c 'permissions.factory-lane.network.enabled=true')
[ -n "$model" ] && args+=(-m "$model")
args+=(--output-last-message "$last" -)

echo "codex-lane: $lane #$issue model=${model:-codex-default} timeout=${timeout}s log=$log" >&2

CLAUDE_PLUGIN_ROOT="$plugin_root" FACTORY_CONFIG="$config" \
  codex "${args[@]}" < "$tmp/prompt.md" > "$log" 2>&1 &
pid=$!

# Watchdog: TERM at the limit, KILL 10s later. Its own output goes nowhere so
# a caller capturing our stdout is never held open by a sleeping watchdog.
(
  sleep "$timeout"
  if kill -0 "$pid" 2>/dev/null; then
    : > "$tmp/timed-out"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 10
    kill -KILL "$pid" 2>/dev/null || true
  fi
) </dev/null >/dev/null 2>&1 &
watchdog=$!

set +e
wait "$pid"
rc=$?
set -e
kill "$watchdog" 2>/dev/null || true
wait "$watchdog" 2>/dev/null || true

if [ -e "$tmp/timed-out" ]; then
  echo "codex-lane: $lane #$issue timed out after ${timeout}s (log: $log)" >&2
  exit 124
fi

# Prefer Codex's final message; fall back to the full transcript.
result=""
for f in "$last" "$log"; do
  [ -s "$f" ] || continue
  result=$(tr -d '\r' < "$f" | grep -E "^[[:space:]\`]*RESULT: $lane #$issue( |\$)" | tail -n 1 \
    | sed -e 's/^[[:space:]`]*//' -e 's/[[:space:]`]*$//' || true)
  [ -n "$result" ] && break
done

if [ -z "$result" ]; then
  echo "codex-lane: $lane #$issue ended (codex exit $rc) without a RESULT line (log: $log)" >&2
  exit 3
fi
[ "$rc" = 0 ] || echo "codex-lane: codex exited $rc but reported a result" >&2
printf '%s\n' "$result"
