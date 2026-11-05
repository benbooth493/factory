#!/usr/bin/env bash
# Tests for scripts/evidence.sh. Run: bash tests/test_evidence.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
ev="$here/../scripts/evidence.sh"
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

export FACTORY_CONFIG="$tmp/factory.json"
echo '{"repo": "acme/app"}' > "$FACTORY_CONFIG"
git init --quiet --bare -b main "$tmp/origin.git"
clone() {
  git clone --quiet "$tmp/origin.git" "$tmp/$1" 2>/dev/null
  git -C "$tmp/$1" config user.email t@example.com
  git -C "$tmp/$1" config user.name test
}
shots() { mkdir -p "$1"; for v in desktop tablet mobile; do echo "$2-$v" > "$1/home-$v-light.png"; done; }

clone a
cd "$tmp/a"
echo hi > README.md && git add README.md && git commit --quiet -m init && git push --quiet origin main
git checkout --quiet -b factory/7
echo ui > page.html && git add page.html && git commit --quiet -m ui && git push --quiet origin factory/7
tested=$(git rev-parse HEAD); short=$(git rev-parse --short HEAD)
before=$(git ls-remote origin refs/heads/factory/7 | cut -f1)

# first push creates the orphan branch; the PR branch and worktree are untouched
shots "$tmp/s1" one
out=$(bash "$ev" push 7 "$tested" "$tmp/s1")
sha=$(echo "$out" | jq -r .sha)
check "dir" "runs/7/qa-$short" "$(echo "$out" | jq -r .dir)"
check "url pinned to evidence commit" "https://github.com/acme/app/blob/$sha/runs/7/qa-$short" "$(echo "$out" | jq -r .url)"
check "branch tip is the evidence commit" "$sha" "$(git ls-remote origin refs/heads/factory-evidence | cut -f1)"
check "evidence branch is an orphan" "no" "$(git merge-base "$sha" "$tested" >/dev/null 2>&1 && echo yes || echo no)"
check "three shots on the branch" "3" "$(git ls-tree -r --name-only "$sha" | grep -c "^runs/7/qa-$short/")"
check "PR branch has no new commit" "$before" "$(git ls-remote origin refs/heads/factory/7 | cut -f1)"
check "worktree still at tested commit" "$tested" "$(git rev-parse HEAD)"
check "worktree clean" "" "$(git status --porcelain)"

# a rejected push refetches and rebuilds on the new tip: b's pre-push hook
# lets a push first, once
clone b
shots "$tmp/s2" two; shots "$tmp/s3" three
cat > "$tmp/b/.git/hooks/pre-push" <<EOF
#!/bin/sh
[ -f "$tmp/raced" ] && exit 0
touch "$tmp/raced"
cd "$tmp/a" && FACTORY_CONFIG="$FACTORY_CONFIG" bash "$ev" push 8 "$tested" "$tmp/s3" >/dev/null
EOF
chmod +x "$tmp/b/.git/hooks/pre-push"
out=$(cd "$tmp/b" && bash "$ev" push 9 "$tested" "$tmp/s2" 2>"$tmp/err")
tip=$(git -C "$tmp/b" ls-remote origin refs/heads/factory-evidence | cut -f1)
check "retry reported" "1" "$(grep -c 'push rejected (attempt 1)' "$tmp/err")"
check "retried push is the tip" "$(echo "$out" | jq -r .sha)" "$tip"
git fetch --quiet origin factory-evidence
check "all three runs kept" "runs/7 runs/8 runs/9" "$(git ls-tree --name-only "$tip" runs/ | xargs)"
check "linear history, no force" "3" "$(git rev-list --count "$tip")"

# real concurrency: three lanes at once all land
for n in 10 11 12; do shots "$tmp/c$n" "c$n"; (bash "$ev" push "$n" "$tested" "$tmp/c$n" >/dev/null 2>&1; echo $? > "$tmp/rc$n") & done
wait
check "concurrent pushes all succeed" "0 0 0" "$(cat "$tmp/rc10" "$tmp/rc11" "$tmp/rc12" | xargs)"
git fetch --quiet origin factory-evidence
check "concurrent runs all on the branch" "6" "$(git ls-tree --name-only origin/factory-evidence runs/ | wc -l | tr -d ' ')"

# bad input
set +e
bash "$ev" push 7 "$tested" "$tmp/empty-missing" 2>/dev/null; rc=$?
set -e
check "missing dir fails" "1" "$rc"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
