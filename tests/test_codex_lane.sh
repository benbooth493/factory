#!/usr/bin/env bash
# Tests for scripts/codex-lane.sh against a fake codex. Run: bash tests/test_codex_lane.sh
# shellcheck disable=SC2015  # test bookkeeping uses a && b || c deliberately
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
lane="$here/../scripts/codex-lane.sh"
plugin="$(cd "$here/.." && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export PATH="$here/fakebin_route:$PATH"
export CODEX_LOG="$tmp/argv" CODEX_PROMPT="$tmp/prompt"
unset FACTORY_CONFIG

git init -q "$tmp/repo"
mkdir -p "$tmp/repo/.claude"
echo '{"repo":"acme/app","codex":{"model":"gpt-config"}}' > "$tmp/repo/.claude/factory.json"
cd "$tmp/repo"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "ok   $1"; }
bad() { fail=$((fail + 1)); echo "FAIL $1"; [ -f "$tmp/err" ] && sed 's/^/     /' "$tmp/err" || true; }
run() { # run <want-exit> [env...] -- <args...>; stdout in $tmp/out
  local want="$1"; shift
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  set +e; env ${envs[@]+"${envs[@]}"} bash "$lane" "$@" > "$tmp/out" 2> "$tmp/err"; got=$?; set -e
  [ "$got" = "$want" ]
}
has_arg() { grep -qxF -- "$1" "$tmp/argv"; }
next_arg() { grep -A1 -xF -- "$1" "$tmp/argv" | sed -n 2p; }

# RESULT from --output-last-message, explicit model wins over config
run 0 FAKE_CODEX=ok -- build 7 --model gpt-6.1-sol && ok "exit 0 with RESULT" || bad "exit 0 with RESULT"
[ "$(cat "$tmp/out")" = "RESULT: build #7 qa pr=12 sha=abc1234" ] && ok "prints only the RESULT line" || bad "RESULT line: $(cat "$tmp/out")"
[ "$(next_arg -m)" = "gpt-6.1-sol" ] && ok "--model passed as -m" || bad "model flag"
has_arg exec && has_arg - && [ "$(next_arg -C)" = "$(pwd -P)" ] && ok "codex exec -C <root> reads prompt from stdin" || bad "exec args: $(tr '\n' ' ' < "$tmp/argv")"
has_arg 'approval_policy="never"' && has_arg 'permissions.factory-lane.network.enabled=true' \
  && ok "non-interactive, network on" || bad "approval/sandbox args"
grep -q "skills/build/SKILL.md" "$tmp/prompt" && grep -q "$plugin" "$tmp/prompt" \
  && ok "prompt names the lane SKILL.md" || bad "prompt: $(head -5 "$tmp/prompt")"
grep -q "issue #7" "$tmp/prompt" && grep -q 'RESULT: build #7' "$tmp/prompt" \
  && ok "prompt names issue and RESULT line" || bad "prompt issue/RESULT"
grep -q "plugin root: $plugin" .claude/factory/codex/build-7.log && ok "CLAUDE_PLUGIN_ROOT exported, log written" \
  || bad "plugin root env / log"

# config model used when no --model; review lane prompt
run 0 FAKE_RESULT="RESULT: review #9 done pr=3 sha=ff" -- review 9 && [ "$(next_arg -m)" = "gpt-config" ] \
  && ok "codex.model from config" || bad "config model: $(next_arg -m)"
grep -q "skills/review/SKILL.md" "$tmp/prompt" && ok "review prompt" || bad "review prompt"

# inherit (a Claude ladder value) and no config model → no -m at all
echo '{"repo":"acme/app"}' > .claude/factory.json
run 0 FAKE_RESULT="RESULT: qa #7 review sha=1" -- qa 7 --model inherit && ! has_arg -m \
  && ok "no -m for inherit / unset" || bad "unexpected -m"

# RESULT only on stdout transcript, wrapped in backticks
# shellcheck disable=SC2016  # literal backticks
run 0 FAKE_CODEX=stdout FAKE_RESULT='`RESULT: build #7 blocked reason="needs key"`' -- build 7 \
  && [ "$(cat "$tmp/out")" = 'RESULT: build #7 blocked reason="needs key"' ] \
  && ok "falls back to transcript, strips backticks" || bad "transcript fallback: $(cat "$tmp/out")"

# RESULT for another card does not count
run 3 FAKE_RESULT="RESULT: build #71 qa pr=1 sha=1" -- build 7 && ok "ignores another issue's RESULT" || bad "other issue RESULT"

# no RESULT → exit 3
run 3 FAKE_CODEX=none -- build 7 && [ ! -s "$tmp/out" ] && ok "missing RESULT → exit 3" || bad "missing RESULT"

# codex fails but reported → still extracted
run 0 FAKE_CODEX=fail -- build 7 && ok "non-zero codex with RESULT still reports" || bad "fail with RESULT"

# timeout enforced
start=$(date +%s)
run 124 FAKE_CODEX=hang -- build 7 --timeout 1 && ok "timeout → exit 124" || bad "timeout exit"
[ $(( $(date +%s) - start )) -lt 10 ] && ok "timeout kills codex promptly" || bad "timeout too slow"
grep -q "timed out" "$tmp/err" && ok "timeout explained" || bad "timeout message"

echo '{"repo":"acme/app","codex":{"timeout_minutes":0}}' > .claude/factory.json
run 64 -- build 7 && ok "rejects zero timeout" || bad "zero timeout"

# usage errors
run 64 -- deploy 7 && ok "rejects unknown lane" || bad "unknown lane"
run 64 -- build seven && ok "rejects non-numeric issue" || bad "bad issue"
run 64 -- build 7 --bogus && ok "rejects unknown option" || bad "unknown option"
run 127 PATH=/usr/bin:/bin -- build 7 && ok "codex missing → 127" || bad "codex missing"

echo; echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
