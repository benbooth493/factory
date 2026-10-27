#!/usr/bin/env bash
# serve-check.sh — is the app up at the base URL, and how do I start it?
#
# Usage (from the repo root or a worktree):
#   serve-check.sh [check] [--base URL]       is it reachable? (default)
#   serve-check.sh start [--base URL] [--wait SECS]
#                                             start it in the background, wait
#                                             (log + pid in <git dir>/factory/)
#
# Settings come from the `ui` block of .claude/factory.json:
#   ui.base_url       default http://localhost:3000
#   ui.start_command  default: package.json "dev" (else "start") with the
#                     repo's package manager. $PORT is exported from base_url.
#   ui.ready_path     path polled for readiness, default /
#
# Prints one JSON object:
#   {"reachable":bool,"base_url":..,"status":<http code|0>,"start_command":..,
#    "pid":<n, start only>,"log":<file, start only>}
# A response of 2xx, 3xx, 401 or 403 counts as up (signed-out apps answer 401).
#
# Exit codes: 0 up · 1 not reachable (start: never came up) · 2 usage ·
#             3 nothing to start (no ui.start_command and no dev/start script)

set -euo pipefail

die() { echo "serve-check: $1" >&2; exit "${2:-2}"; }

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"

cfg() {
  [ -f "$config" ] || return 0
  jq -r "$1 // empty" "$config" 2>/dev/null || true
}

cmd=check
case "${1:-}" in
  check|start) cmd="$1"; shift ;;
esac
base="" wait_secs=60
while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || die "--base needs a URL"; base="$2"; shift 2 ;;
    --wait) [ $# -ge 2 ] || die "--wait needs seconds"; wait_secs="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$base" ] || base=$(cfg .ui.base_url)
[ -n "$base" ] || base="http://localhost:3000"
base="${base%/}"
ready_path=$(cfg .ui.ready_path)
[ -n "$ready_path" ] || ready_path="/"

# Port from the base URL (explicit, else the scheme's default).
port_of() {
  local hostport
  hostport=$(printf '%s' "$1" | sed -E 's#^[a-z]+://##; s#/.*$##; s#^.*@##')
  case "$hostport" in
    *:*) printf '%s' "${hostport##*:}" ;;
    *) case "$1" in https://*) printf 443 ;; *) printf 80 ;; esac ;;
  esac
}

guess_start() {
  local pm=npm script=""
  [ -f "$root/package.json" ] || return 0
  if jq -e '.scripts.dev' "$root/package.json" >/dev/null 2>&1; then script=dev
  elif jq -e '.scripts.start' "$root/package.json" >/dev/null 2>&1; then script=start
  else return 0
  fi
  if [ -f "$root/pnpm-lock.yaml" ]; then pm=pnpm
  elif [ -f "$root/yarn.lock" ]; then pm=yarn
  elif [ -f "$root/bun.lockb" ] || [ -f "$root/bun.lock" ]; then pm=bun
  fi
  printf '%s run %s' "$pm" "$script"
}

start_cmd=$(cfg .ui.start_command)
[ -n "$start_cmd" ] || start_cmd=$(guess_start)

http_status() {
  curl -s -o /dev/null -m 5 -w '%{http_code}' "$base$ready_path" 2>/dev/null || true
}

is_up() {
  case "$1" in
    2??|3??|401|403) return 0 ;;
    *) return 1 ;;
  esac
}

report() {
  # report <reachable> <status> [pid] [log]
  jq -nc --argjson up "$1" --arg base "$base" --argjson status "${2:-0}" \
    --arg start "$start_cmd" --arg pid "${3:-}" --arg log "${4:-}" '
    {reachable: $up, base_url: $base, status: $status,
     start_command: (if $start == "" then null else $start end)}
    + (if $pid == "" then {} else {pid: ($pid | tonumber), log: $log} end)'
}

status=$(http_status)
status=${status:-0}
case "$status" in ''|*[!0-9]*) status=0 ;; esac

if is_up "$status"; then
  report true "$status"
  exit 0
fi

if [ "$cmd" = check ]; then
  report false "$status"
  if [ -n "$start_cmd" ]; then
    echo "serve-check: nothing answering at $base. Start it with: PORT=$(port_of "$base") $start_cmd" >&2
    echo "serve-check: or run: serve-check.sh start" >&2
  else
    echo "serve-check: nothing answering at $base, and no way to start it is known." >&2
    echo "serve-check: set ui.start_command (and ui.base_url) in $config" >&2
  fi
  exit 1
fi

# start
[ -n "$start_cmd" ] || die "no ui.start_command in $config and no dev/start script in package.json" 3
# Inside the git dir so the log and pid can never be committed.
log_dir="$(git rev-parse --absolute-git-dir 2>/dev/null || echo "$root/.claude")/factory"
mkdir -p "$log_dir"
log="$log_dir/serve.log"
PORT=$(port_of "$base")
export PORT
# Detach fully (stdin too) so callers capturing our output are not held open.
(cd "$root" && exec nohup bash -c "$start_cmd" </dev/null >"$log" 2>&1) &
pid=$!
echo "$pid" >"$log_dir/serve.pid"

waited=0
while [ "$waited" -lt "$wait_secs" ]; do
  sleep 1
  waited=$((waited + 1))
  status=$(http_status)
  case "$status" in ''|*[!0-9]*) status=0 ;; esac
  if is_up "$status"; then
    report true "$status" "$pid" "$log"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    break
  fi
done
report false "$status" "$pid" "$log"
echo "serve-check: $base did not come up after ${waited}s; see $log" >&2
exit 1
