#!/usr/bin/env bash
# Tests for scripts/merge-gate.sh: real git repos, fake gh. Run: bash tests/test_merge_gate.sh
# shellcheck disable=SC2015  # test bookkeeping uses a && b || c deliberately
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

# Jev risk veto (canned gateway responses, no network)
fx="$here/fixtures/jev"
jev_config() { # jev_config <mode>
  write_config "test -f b.txt"
  jq --arg m "$1" '.jev = {enabled: true, points: {risk_veto: $m}}' .claude/factory.json > c && mv c .claude/factory.json
}
export FACTORY_JEV_LOG="$tmp/jev.jsonl" AI_GATEWAY_API_KEY=vck_test

jev_config live
run "live veto holds a sensitive change" 4 FAKE_HEAD="$head" FACTORY_JEV_FAKE_RESPONSE="$fx/risk_sensitive.json"
if grep -q "pr merge" "$GH_LOG"; then fail=$((fail + 1)); echo "FAIL merged despite veto"
else pass=$((pass + 1)); echo "ok   no merge when held"; fi
if grep -q -- "--add-label factory:needs-human" "$GH_LOG"; then pass=$((pass + 1)); echo "ok   held PR labelled needs-human"
else fail=$((fail + 1)); echo "FAIL needs-human label missing"; fi

run "live veto lets a safe change merge" 0 FAKE_HEAD="$head" FACTORY_JEV_FAKE_RESPONSE="$fx/risk_safe.json"
grep -q "pr merge" "$GH_LOG" && { pass=$((pass + 1)); echo "ok   safe change merged"; } || { fail=$((fail + 1)); echo "FAIL safe change not merged"; }

jev_config shadow
run "shadow veto never blocks" 0 FAKE_HEAD="$head" FACTORY_JEV_FAKE_RESPONSE="$fx/risk_sensitive.json"
grep -q "pr merge" "$GH_LOG" && { pass=$((pass + 1)); echo "ok   shadow merged and logged"; } || { fail=$((fail + 1)); echo "FAIL shadow blocked"; }

jev_config live
run "jev unavailable fails open" 0 FAKE_HEAD="$head" AI_GATEWAY_API_KEY= FACTORY_JEV_FAKE_RESPONSE=

[ "$(grep -c '"kind": "jev"' "$tmp/jev.jsonl")" -eq 3 ] && { pass=$((pass + 1)); echo "ok   three decisions logged"; } \
  || { fail=$((fail + 1)); echo "FAIL jev log"; cat "$tmp/jev.jsonl"; }
unset FACTORY_JEV_LOG AI_GATEWAY_API_KEY

write_config "test -f b.txt" false
run "auto_merge off leaves it for a human" 0 FAKE_HEAD="$head"
if grep -q "pr merge" "$GH_LOG"; then fail=$((fail + 1)); echo "FAIL merged with auto_merge off"
else pass=$((pass + 1)); echo "ok   no merge with auto_merge off"; fi

# Merge policy: who may merge what. Every hold is exit 4 with needs-human.
policy_config() { # policy_config <jq filter applied to a passing config>
  write_config "test -f b.txt"
  jq "$1" .claude/factory.json > c && mv c .claude/factory.json
}
held() { # held <name>: last run held, did not merge, labelled and explained
  if ! grep -q "pr merge" "$GH_LOG" && grep -q -- "--add-label factory:needs-human" "$GH_LOG" \
     && grep -q "held this PR for a human" "$GH_LOG"; then
    pass=$((pass + 1)); echo "ok   $1: held, labelled, explained"
  else fail=$((fail + 1)); echo "FAIL $1: not held cleanly"; cat "$GH_LOG"; fi
}
merged() { # merged <name>
  grep -q "pr merge" "$GH_LOG" && { pass=$((pass + 1)); echo "ok   $1: merged"; } \
    || { fail=$((fail + 1)); echo "FAIL $1: not merged"; cat "$tmp/out"; }
}

policy_config '.merge_policy.auto = {feature: false, bug: true}'
run "kind not allowed to auto-merge" 4 FAKE_HEAD="$head" FAKE_PR_LABELS='["kind:feature"]'
held "kind:feature"
if grep -q "running:" "$tmp/out"; then fail=$((fail + 1)); echo "FAIL verify ran for a held PR"
else pass=$((pass + 1)); echo "ok   policy holds before spending verify"; fi
run "allowed kind merges" 0 FAKE_HEAD="$head" FAKE_PR_LABELS='["kind:bug"]'
merged "kind:bug"

policy_config '.merge_policy.auto = ["bug"]'
run "kind read from the closed issue" 0 FAKE_HEAD="$head" FAKE_CLOSES='[3]' FAKE_LABELS='["kind:bug"]'
merged "issue kind:bug"
run "unlisted kind is held" 4 FAKE_HEAD="$head" FAKE_CLOSES='[3]' FAKE_LABELS='["kind:refactor"]'
run "no kind at all is held under a list" 4 FAKE_HEAD="$head"

policy_config '.merge_policy.auto = false'
run "auto false holds everything" 4 FAKE_HEAD="$head"

policy_config '.'
run "default block label do-not-merge" 4 FAKE_HEAD="$head" FAKE_PR_LABELS='["do-not-merge"]'
policy_config '.merge_policy.require_labels = ["approved-by-owner"]'
run "missing required label" 4 FAKE_HEAD="$head"
run "required label present" 0 FAKE_HEAD="$head" FAKE_PR_LABELS='["approved-by-owner"]'

policy_config '.merge_policy.max_files = 2'
run "too many files" 4 FAKE_HEAD="$head" \
  FAKE_FILES='[{"path":"a","additions":1,"deletions":0},{"path":"b","additions":1,"deletions":0},{"path":"c","additions":1,"deletions":0}]'
policy_config '.merge_policy.max_lines = 100'
run "too many lines" 4 FAKE_HEAD="$head" FAKE_FILES='[{"path":"src/x.ts","additions":90,"deletions":20}]'
run "lockfile lines do not count" 0 FAKE_HEAD="$head" \
  FAKE_FILES='[{"path":"src/x.ts","additions":10,"deletions":0},{"path":"package-lock.json","additions":5000,"deletions":0}]'

# Migrations
mig_files='[{"path":"b.txt","additions":1,"deletions":0},{"path":"supabase/migrations/001_add.sql","additions":3,"deletions":0}]'
policy_config '.'
run "migration held when not allowed" 4 FAKE_HEAD="$head" FAKE_FILES="$mig_files"
held "migration"
grep -q "supabase/migrations/001_add.sql" "$tmp/out" && { pass=$((pass + 1)); echo "ok   hold names the migration"; } \
  || { fail=$((fail + 1)); echo "FAIL hold does not name the migration"; cat "$tmp/out"; }

policy_config '.migrations = {allowed: true}'
run "allowed migration without a command is held" 4 FAKE_HEAD="$head" FAKE_FILES="$mig_files"

# The command proves it ran after the merge by finding the merge in the gh log.
policy_config ".migrations = {allowed: true, command: \"grep -q 'pr merge' '$GH_LOG' && touch '$tmp/migrated'\"}"
rm -f "$tmp/migrated"
run "allowed migration merges, then applies" 0 FAKE_HEAD="$head" FAKE_FILES="$mig_files"
merged "allowed migration"
[ -f "$tmp/migrated" ] && { pass=$((pass + 1)); echo "ok   migration ran after the merge"; } \
  || { fail=$((fail + 1)); echo "FAIL migration did not run after merge"; cat "$tmp/out"; }

rm -f "$tmp/migrated"
run "no migration files, no migration run" 0 FAKE_HEAD="$head"
[ ! -f "$tmp/migrated" ] && { pass=$((pass + 1)); echo "ok   command skipped without migrations"; } \
  || { fail=$((fail + 1)); echo "FAIL migration ran for a PR without migrations"; }

policy_config '.migrations = {allowed: true, command: "echo boom; exit 3"}'
run "failed migration after merge is loud" 5 FAKE_HEAD="$head" FAKE_FILES="$mig_files"
merged "failed migration"
if grep -q "MIGRATION FAILED" "$GH_LOG" && grep -q "boom" "$GH_LOG" \
   && grep -q -- "--add-label factory:needs-human" "$GH_LOG"; then
  pass=$((pass + 1)); echo "ok   failed migration posts a loud comment"
else fail=$((fail + 1)); echo "FAIL no loud comment"; cat "$GH_LOG"; fi

policy_config ".auto_merge = false | .migrations = {allowed: true, command: \"touch '$tmp/migrated'\"}"
rm -f "$tmp/migrated"
run "auto_merge off never migrates" 0 FAKE_HEAD="$head" FAKE_FILES="$mig_files"
[ ! -f "$tmp/migrated" ] && { pass=$((pass + 1)); echo "ok   no migration without a merge"; } \
  || { fail=$((fail + 1)); echo "FAIL migrated without merging"; }

jq '.verify_commands = []' .claude/factory.json > c && mv c .claude/factory.json
run "refuses with no verify_commands" 2 FAKE_HEAD="$head"

if ! git worktree list | grep -q gate-; then
  pass=$((pass + 1)); echo "ok   gate worktrees cleaned up"
else
  fail=$((fail + 1)); echo "FAIL leftover gate worktree"
fi

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
