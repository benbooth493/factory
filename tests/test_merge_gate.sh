#!/usr/bin/env bash
# Tests for scripts/merge-gate.sh: real git repos, fake gh. Run: bash tests/test_merge_gate.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
gate="$here/../scripts/merge-gate.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export PATH="$here/fakebin:$PATH"
export GH_LOG="$tmp/gh.log"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

# origin with main + a feature branch
git init -q --bare -b main "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/work" 2>/dev/null
cd "$tmp/work"
echo base > a.txt && git add a.txt && git commit -qm base && git push -q origin main
git checkout -q -b factory/3
echo feature > b.txt && git add b.txt && git commit -qm feature && git push -q origin factory/3
head=$(git rev-parse HEAD)
git checkout -q main

mkdir -p .claude
write_config() {
  jq -n --arg v "$1" --argjson auto "${2:-true}" \
    '{repo:"acme/app", base_branch:"main", verify_commands:[$v], auto_merge:$auto}' > .claude/factory.json
}

pass=0; fail=0
run() { # run <name> <expected-exit> [env...]
  local name="$1" want="$2"; shift 2
  : > "$GH_LOG"
  set +e; env "$@" bash "$gate" 3 "$head" > "$tmp/out" 2>&1; got=$?; set -e
  if [ "$got" = "$want" ]; then pass=$((pass + 1)); echo "ok   $name"
  else fail=$((fail + 1)); echo "FAIL $name (exit $got, want $want)"; sed 's/^/     /' "$tmp/out"; fi
}

write_config "test -f b.txt"
run "merges when head matches and verify passes" 0 FAKE_HEAD="$head"
if grep -q -- "pr merge 3 --repo acme/app --squash --delete-branch --match-head-commit $head" "$GH_LOG"; then
  pass=$((pass + 1)); echo "ok   merge pinned to reviewed sha"
else
  fail=$((fail + 1)); echo "FAIL merge not pinned"; cat "$GH_LOG"
fi

run "refuses when head moved since review" 1 FAKE_HEAD=0000000000000000000000000000000000000000
run "refuses with failing check" 1 FAKE_HEAD="$head" FAKE_CHECK=FAILURE
run "refuses with pending check" 1 FAKE_HEAD="$head" FAKE_CHECK=PENDING
run "refuses with conflicts" 1 FAKE_HEAD="$head" FAKE_MERGEABLE=CONFLICTING

write_config "false"
run "refuses when verify fails" 1 FAKE_HEAD="$head"

write_config "test -f b.txt" false
run "auto_merge off leaves it for a human" 0 FAKE_HEAD="$head"
if grep -q "pr merge" "$GH_LOG"; then fail=$((fail + 1)); echo "FAIL merged with auto_merge off"
else pass=$((pass + 1)); echo "ok   no merge with auto_merge off"; fi

jq '.verify_commands = []' .claude/factory.json > c && mv c .claude/factory.json
run "refuses with no verify_commands" 2 FAKE_HEAD="$head"

if ! git worktree list | grep -q gate-; then
  pass=$((pass + 1)); echo "ok   gate worktrees cleaned up"
else
  fail=$((fail + 1)); echo "FAIL leftover gate worktree"
fi

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
