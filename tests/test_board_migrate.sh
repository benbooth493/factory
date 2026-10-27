#!/usr/bin/env bash
# Tests for scripts/board-migrate.sh against a stateful fake gh. Run: bash tests/test_board_migrate.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
migrate="$here/../scripts/board-migrate.sh"
fx="$here/fixtures/migrate"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export PATH="$here/fakebin:$PATH"
export GH_LOG="$tmp/gh.log"
export FACTORY_CONFIG="$tmp/factory.json"
export FAKE_BOARD="$tmp/board.json"
echo '{"owner": "acme", "project_number": 5, "repo": "acme/app"}' > "$FACTORY_CONFIG"
cd "$tmp"  # not a git repo: backups land in $tmp/.claude/factory

pass=0; fail=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    pass=$((pass + 1)); echo "ok   $name"
  else
    fail=$((fail + 1)); echo "FAIL $name"; echo "     expected: $expected"; echo "     actual:   $actual"
  fi
}
status_of() { jq -r --arg id "$1" '.items[] | select(.id == $id) | .status // "none"' "$FAKE_BOARD"; }
options() { jq -r '[.options[].name] | join(",")' "$FAKE_BOARD"; }

# Dry run: a plan, and nothing written.
cp "$fx/legacy.json" "$FAKE_BOARD"
: > "$GH_LOG"
out=$("$migrate" --dry-run)
check "dry run lists missing columns" "1" "$(grep -c 'add columns: Backlog, Build, QA, Review, Blocked' <<<"$out")"
check "dry run maps Building" "1" "$(grep -c 'move 1 card(s) Building -> Build: #2' <<<"$out")"
check "dry run maps Todo" "1" "$(grep -c 'move 1 card(s) Todo -> Ready: #1' <<<"$out")"
check "dry run maps In review" "1" "$(grep -c 'In review -> Review: #4' <<<"$out")"
check "dry run leaves unknown columns" "1" "$(grep -c 'leave 1 card(s) in Parked' <<<"$out")"
check "dry run changes nothing" "$(cat "$fx/legacy.json" | jq -c .)" "$(jq -c . "$FAKE_BOARD")"
check "dry run makes no writes" "0" "$(grep -c -e 'item-edit' -e 'updateProjectV2Field' "$GH_LOG" || true)"

# Real run.
out=$("$migrate")
check "factory columns first, every old option kept" \
  "Backlog,Ready,Build,QA,Review,Blocked,Done,Todo,Building,In progress,In review,Parked" "$(options)"
check "existing option keeps its colour and description" "BLUE Groomed" \
  "$(jq -r '.options[] | select(.name == "Ready") | "\(.color) \(.description)"' "$FAKE_BOARD")"
check "Todo -> Ready" "Ready" "$(status_of I1)"
check "Building -> Build" "Build" "$(status_of I2)"
check "In progress -> Build" "Build" "$(status_of I3)"
check "In review -> Review (PR card too)" "Review" "$(status_of I4)"
check "Done restored after the rewrite" "Done" "$(status_of I5)"
check "Ready restored after the rewrite" "Ready" "$(status_of I6)"
check "unknown column restored, not dropped" "Parked" "$(status_of I7)"
check "card without a column left alone" "none" "$(status_of I8)"
check "reports read-back" "1" "$(grep -c 'every card reads back' <<<"$out")"
check "backup written" "1" "$(find "$tmp/.claude/factory" -name 'board-backup-5-*.json' | wc -l | tr -d ' ')"

# Second run is a no-op.
: > "$GH_LOG"
out=$("$migrate")
check "idempotent" "1" "$(grep -c 'nothing to do' <<<"$out")"
check "idempotent makes no writes" "0" "$(grep -c -e 'item-edit' -e 'updateProjectV2Field' "$GH_LOG" || true)"

# Complete board, nothing to do.
cp "$fx/complete.json" "$FAKE_BOARD"
check "complete board" "  nothing to do" "$("$migrate" | tail -n1)"

# Configured column names and aliases.
cp "$fx/legacy.json" "$FAKE_BOARD"
jq '.columns = {build: "Building"} | .board_migrate.aliases = {"Parked": "blocked"}' "$FACTORY_CONFIG" > "$tmp/c" \
  && mv "$tmp/c" "$FACTORY_CONFIG"
"$migrate" >/dev/null
check "configured name is the build column" "Building" "$(status_of I2)"
check "In progress -> configured build column" "Building" "$(status_of I3)"
check "config alias" "Blocked" "$(status_of I7)"
check "no duplicate Build option" "0" "$(jq '[.options[] | select(.name == "Build")] | length' "$FAKE_BOARD")"

# Only moves needed (all columns exist): no option rewrite.
cp "$fx/complete.json" "$FAKE_BOARD"
jq '.options += [{"id":"o_todo","name":"Todo","color":"GRAY","description":""}] | .items[0].status = "Todo"' \
  "$FAKE_BOARD" > "$tmp/b" && mv "$tmp/b" "$FAKE_BOARD"
echo '{"owner": "acme", "project_number": 5, "repo": "acme/app"}' > "$FACTORY_CONFIG"
: > "$GH_LOG"
"$migrate" >/dev/null
check "move without rewriting options" "0" "$(grep -c 'updateProjectV2Field' "$GH_LOG" || true)"
check "moved" "Ready" "$(status_of I1)"
check "Todo option kept even when empty" "1" "$(jq '[.options[] | select(.name == "Todo")] | length' "$FAKE_BOARD")"

# A failed option update stops before any card is touched.
cp "$fx/legacy.json" "$FAKE_BOARD"
if FAKE_GRAPHQL_FAIL=1 "$migrate" >/dev/null 2>&1; then r=0; else r=$?; fi
check "graphql failure exits 1" "1" "$r"
check "graphql failure leaves cards alone" "Building" "$(status_of I2)"

if "$migrate" --bogus >/dev/null 2>&1; then r=0; else r=$?; fi
check "bad usage exits 2" "2" "$r"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
