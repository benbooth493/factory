#!/usr/bin/env bash
# Tests for scripts/board.sh against a fake gh. Run: bash tests/test_board.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
board="$here/../scripts/board.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export PATH="$here/fakebin:$PATH"
export GH_LOG="$tmp/gh.log"
export FACTORY_CONFIG="$tmp/factory.json"
cat > "$FACTORY_CONFIG" <<'JSON'
{"owner": "acme", "project_number": 5, "repo": "acme/app", "verify_commands": ["true"]}
JSON

pass=0; fail=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    pass=$((pass + 1)); echo "ok   $name"
  else
    fail=$((fail + 1)); echo "FAIL $name"; echo "     expected: $expected"; echo "     actual:   $actual"
  fi
}

# items: only issues from the configured repo
check "items filters to repo issues" "1 2 3 7 9" \
  "$("$board" items | jq -r '[.[].number] | sort | map(tostring) | join(" ")')"

check "items by column" "1 2 3" \
  "$("$board" items ready | jq -r '[.[].number] | sort | map(tostring) | join(" ")')"

check "next skips claimed, oldest first" "2 3" \
  "$("$board" next ready 5 | jq -r '[.[].number] | map(tostring) | join(" ")')"

check "next respects n" "2" \
  "$("$board" next ready 1 | jq -r '.[0].number')"

check "next on empty column" "0" \
  "$("$board" next build 3 | jq 'length')"

check "status counts" "Ready 3|QA 1|Done 1|in-flight 2" \
  "$("$board" status | awk '$2>0{printf "%s%s %s", (n++?"|":""), $1, $2}')"

# column renames from config
jq '.columns = {"build": "In progress"}' "$FACTORY_CONFIG" > "$tmp/c" && mv "$tmp/c" "$FACTORY_CONFIG"
check "column override" "In progress" "$("$board" column build)"
check "column default" "QA" "$("$board" column qa)"
jq 'del(.columns)' "$FACTORY_CONFIG" > "$tmp/c" && mv "$tmp/c" "$FACTORY_CONFIG"

# move resolves item, field and option ids
: > "$GH_LOG"
"$board" move 3 build >/dev/null
check "move edits the right item" \
  "gh project item-edit --id PVTI_1 --project-id PVT_project --field-id PVTSSF_status --single-select-option-id o_build" \
  "$(grep 'item-edit' "$GH_LOG")"

if "$board" move 99 build >/dev/null 2>&1; then r=0; else r=1; fi
check "move unknown issue fails" "1" "$r"

# claim is a mutex on the wip label
: > "$GH_LOG"
FAKE_LABELS='[]' "$board" claim 3 >/dev/null
check "claim adds label" "1" "$(grep -c -- '--add-label factory:wip' "$GH_LOG")"
if FAKE_LABELS='["factory:wip"]' "$board" claim 3 >/dev/null 2>&1; then r=0; else r=1; fi
check "claim refuses when held" "1" "$r"

# bounce counting
check "bounces none" "0" "$(FAKE_LABELS='["bug"]' "$board" bounces 3)"
check "bounces max" "2" "$(FAKE_LABELS='["factory:bounce-1","factory:bounce-2"]' "$board" bounces 3)"
check "bounce increments" "3" "$(FAKE_LABELS='["factory:bounce-2"]' "$board" bounce 3)"

# doctor passes with a complete board
check "doctor ready" "ready" "$("$board" doctor | tail -n1)"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
