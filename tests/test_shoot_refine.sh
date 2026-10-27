#!/usr/bin/env bash
# Tests for skills/ui-refine-loop/refine.sh. Run: bash tests/test_shoot_refine.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
refine="$here/../skills/ui-refine-loop/refine.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

pass=0; fail=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    pass=$((pass + 1)); echo "ok   $name"
  else
    fail=$((fail + 1)); echo "FAIL $name"; echo "     expected: $expected"; echo "     actual:   $actual"
  fi
}

export HOME="$tmp/home"
mkdir -p "$HOME"
repo="$tmp/repo"
mkdir -p "$repo"
cd "$repo"
git init --quiet -b main
git config user.email t@example.com
git config user.name test
echo hi > README.md
git add README.md
git commit --quiet -m init
export FACTORY_CONFIG="$tmp/factory.json"
echo '{"repo": "acme/app"}' > "$FACTORY_CONFIG"

# detect: nothing installed → exit 1, both named
set +e
out=$(bash "$refine" detect 2>"$tmp/err"); rc=$?
set -e
check "detect fails without skills" "1" "$rc"
check "detect names missing skills" "impeccable humanizer" "$(echo "$out" | jq -r '.missing | join(" ")')"
check "detect explains on stderr" "1" "$(grep -c 'missing required skill' "$tmp/err")"
check "default taste file" ".claude/factory/taste.md" "$(echo "$out" | jq -r .taste_file)"

# detect: project skill + home skill with an older detector layout
mkdir -p "$repo/.claude/skills/impeccable/scripts" "$HOME/.agents/skills/humanizer"
touch "$repo/.claude/skills/impeccable/SKILL.md" "$repo/.claude/skills/impeccable/scripts/detect.mjs"
touch "$HOME/.agents/skills/humanizer/SKILL.md"
echo '{"repo": "acme/app", "ui": {"taste_file": "docs/taste.md"}}' > "$FACTORY_CONFIG"
out=$(bash "$refine" detect)
check "detect finds impeccable in project" "$repo/.claude/skills/impeccable" "$(echo "$out" | jq -r .impeccable)"
check "detect finds humanizer in home" "$HOME/.agents/skills/humanizer" "$(echo "$out" | jq -r .humanizer)"
check "detect node detector" "node $repo/.claude/skills/impeccable/scripts/detect.mjs --json" "$(echo "$out" | jq -r .impeccable_detect)"
check "detect nothing missing" "0" "$(echo "$out" | jq '.missing | length')"
check "taste file from config" "docs/taste.md" "$(echo "$out" | jq -r .taste_file)"

# launcher layout wins over detect.mjs
printf '#!/bin/sh\n' > "$repo/.claude/skills/impeccable/scripts/impeccable"
chmod +x "$repo/.claude/skills/impeccable/scripts/impeccable"
check "detect launcher" "$repo/.claude/skills/impeccable/scripts/impeccable detect --json" \
  "$(bash "$refine" detect | jq -r .impeccable_detect)"

# open: worktree under .claude/worktrees, run dir in git dir, idempotent
out=$(bash "$refine" open settings-billing)
wt=$(echo "$out" | jq -r .worktree)
run=$(echo "$out" | jq -r .run)
check "open worktree path" "$repo/.claude/worktrees/refine-settings-billing" "$wt"
check "open branch" "factory/refine-settings-billing" "$(git -C "$wt" rev-parse --abbrev-ref HEAD)"
check "open base branch" "main" "$(echo "$out" | jq -r .base_branch)"
check "open base sha" "$(git rev-parse HEAD)" "$(echo "$out" | jq -r .base_sha)"
check "run dir has shots" "yes" "$([ -d "$run/shots" ] && echo yes || echo no)"
check "run dir is not in the work tree" "" "$(git -C "$wt" status --porcelain)"
check "open is idempotent" "$wt" "$(bash "$refine" open settings-billing | jq -r .worktree)"
set +e
bash "$refine" open "Bad Slug" >/dev/null 2>&1; rc=$?
set -e
check "open rejects bad slug" "2" "$rc"

# table: copies shots, commits, links pinned to the commit
png() { printf '\x89PNG\r\n\x1a\n%s' "$1" > "$2"; }
for vp in desktop mobile; do
  for th in light dark; do
    png "b$vp$th" "$run/shots/round-0-$vp-$th.png"
    png "a$vp$th" "$run/shots/round-2-$vp-$th.png"
  done
done
rm "$run/shots/round-2-mobile-dark.png"
echo change > "$wt/x.txt"; git -C "$wt" add x.txt; git -C "$wt" commit --quiet -m "ui-refine: fix"
code_sha=$(git -C "$wt" rev-parse --short HEAD)
table=$(bash "$refine" table --worktree "$wt" --run "$run" --slug settings-billing --final round-2)
sha=$(git -C "$wt" rev-parse HEAD)
check "table committed shots" "ui-refine settings-billing: before/after screenshots" "$(git -C "$wt" log -1 --format=%s)"
check "table committed 7 files" "7" "$(git -C "$wt" show --name-only --format= HEAD | grep -c '^docs/factory/refine/settings-billing/')"
check "table rows" "6" "$(echo "$table" | wc -l | tr -d ' ')"
check "table header shas" "| | Before \`$(git rev-parse --short HEAD)\` | After \`$code_sha\` |" "$(echo "$table" | head -1)"
check "table pinned blob raw url" "1" \
  "$(echo "$table" | grep -c "https://github.com/acme/app/blob/$sha/docs/factory/refine/settings-billing/after-desktop-light.png?raw=true")"
check "table marks missing shot" "1" "$(echo "$table" | grep -c 'Mobile 390 dark .*| not shot |$')"

# re-run without new shots: no empty commit
before=$(git -C "$wt" rev-parse HEAD)
bash "$refine" table --worktree "$wt" --run "$run" --slug settings-billing --final round-2 >/dev/null
check "table re-run makes no commit" "$before" "$(git -C "$wt" rev-parse HEAD)"

set +e
bash "$refine" table --worktree "$wt" --run "$run" --slug x --final round-9 >/dev/null 2>&1; rc=$?
bash "$refine" table --worktree "$wt" >/dev/null 2>&1; rc2=$?
bash "$refine" nope >/dev/null 2>&1; rc3=$?
set -e
check "table with no final shots fails" "3" "$rc"
check "failed table leaves no files" "no" "$([ -d "$wt/docs/factory/refine/x" ] && echo yes || echo no)"
check "table usage" "2" "$rc2"
check "unknown command" "2" "$rc3"

git worktree remove --force "$wt"
echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
