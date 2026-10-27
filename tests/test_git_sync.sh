#!/usr/bin/env bash
# Tests for scripts/git-sync.sh against throwaway git repos. Run: bash tests/test_git_sync.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
sync="$here/../scripts/git-sync.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export GIT_CONFIG_GLOBAL="$tmp/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global init.defaultBranch main
git config --global pull.rebase true   # git-sync must merge regardless
unset FACTORY_CONFIG

pass=0; fail=0
check() { # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); echo "ok   $1"
  else fail=$((fail + 1)); echo "FAIL $1"; echo "     expected: $2"; echo "     actual:   $3"; fi
}
code() { set +e; "$@" > "$tmp/out" 2> "$tmp/err"; echo $?; set -e; }

# origin with main; two clones
git init -q --bare "$tmp/origin.git"
git clone -q "$tmp/origin.git" "$tmp/a" 2>/dev/null
cd "$tmp/a"
echo one > a.txt && git add a.txt && git commit -qm base && git push -q origin main
git clone -q "$tmp/origin.git" "$tmp/b"

# --- status JSON shape ----------------------------------------------------
git checkout -q -b feature/x && git push -q -u origin feature/x 2>/dev/null
echo two >> a.txt
echo new > new.txt
echo staged > s.txt && git add s.txt
echo 'SECRET=1' > .env
echo 'k = "AKIAABCDEFGHIJKLMNOP"' > conf.py
printf 'build/\n' > .gitignore && mkdir build && echo x > build/out.js
"$sync" status > "$tmp/status.json"
check "status keys" \
  "ahead behind branch clean conflicted detached in_progress in_sync protected secrets staged unstaged untracked upstream" \
  "$(jq -r 'keys | join(" ")' "$tmp/status.json")"
check "status branch/upstream" "feature/x origin/feature/x false false" \
  "$(jq -r '"\(.branch) \(.upstream) \(.protected) \(.detached)"' "$tmp/status.json")"
check "status file lists" "s.txt|a.txt|.env .gitignore conf.py new.txt" \
  "$(jq -r '"\(.staged | join(" "))|\(.unstaged | join(" "))|\(.untracked | sort | join(" "))"' "$tmp/status.json")"
check "status ignores gitignored build output" "false" \
  "$(jq '[.staged, .unstaged, .untracked] | flatten | any(startswith("build/"))' "$tmp/status.json")"
check "status flags secrets by name and content" ".env:secret file conf.py:AWS access key" \
  "$(jq -r '[.secrets[] | "\(.path):\(.why)"] | join(" ")' "$tmp/status.json")"
check "status not clean" "false false" "$(jq -r '"\(.clean) \(.in_sync)"' "$tmp/status.json")"
rm -f .env conf.py new.txt .gitignore && rm -rf build && git checkout -q -- a.txt && git rm -q --cached s.txt && rm s.txt
check "clean tree is in sync" "true true 0 0" \
  "$("$sync" status --fetch | jq -r '"\(.clean) \(.in_sync) \(.ahead) \(.behind)"')"

# --- push refusals --------------------------------------------------------
echo three >> a.txt && git commit -qam three
for f in --force -f --force-with-lease +feature/x --delete; do
  check "push refuses $f" "5" "$(code "$sync" push "$f")"
done
check "refusal says why" "1" "$(grep -c "refused: deleting remote branches" "$tmp/err")"
check "refused push sent nothing" "1" "$(git rev-list --count origin/feature/x..HEAD)"

check "push feature branch" "0" "$(code "$sync" push)"
check "push result" "pushed origin/feature/x" "$(jq -r '"\(.status) \(.upstream)"' "$tmp/out")"
check "remote got the commit" "$(git rev-parse HEAD)" "$(git ls-remote "$tmp/origin.git" refs/heads/feature/x | cut -f1)"

git checkout -q -b feature/new
echo n > n.txt && git add n.txt && git commit -qm n
check "push new branch sets upstream" "0 origin/feature/new" \
  "$(code "$sync" push) $(git rev-parse --abbrev-ref '@{upstream}')"

git checkout -q main
echo m >> a.txt && git commit -qam on-main
main_remote=$(git ls-remote "$tmp/origin.git" refs/heads/main | cut -f1)
check "push refuses main" "5" "$(code "$sync" push)"
check "main untouched on remote" "$main_remote" "$(git ls-remote "$tmp/origin.git" refs/heads/main | cut -f1)"

mkdir -p .claude && echo '{"base_branch": "develop", "protected_branches": ["release"]}' > .claude/factory.json
git checkout -q -b release
check "push refuses configured protected branch" "5" "$(code "$sync" push)"
check "status marks it protected" "true" "$("$sync" status | jq .protected)"
git checkout -q -b develop
check "push refuses base_branch" "5" "$(code "$sync" push)"
git checkout -q -b topic --track origin/main 2>/dev/null
check "push refuses a branch tracking main" "5" "$(code "$sync" push)"
git checkout -q main && git branch -q -D release develop topic && rm -rf .claude

# --- move work off main ---------------------------------------------------
echo wip > wip.txt
check "branch moves work off main" "0" "$(code "$sync" branch fix/thing)"
check "now on new branch with the commit" "fix/thing on-main wip.txt" \
  "$(git symbolic-ref --short HEAD) $(git log -1 --format=%s) $(git ls-files --others --exclude-standard)"
check "main reset to upstream" "$main_remote" "$(git rev-parse main)"
check "branch refuses a protected name" "5" "$(code "$sync" branch master)"
rm wip.txt

# --- pull merges, never rebases ------------------------------------------
cd "$tmp/b"
git fetch -q && git checkout -q -b feature/x origin/feature/x
echo theirs > b.txt && git add b.txt && git commit -qm theirs && git push -q
cd "$tmp/a"
git checkout -q feature/x
echo ours > c.txt && git add c.txt && git commit -qm ours
ours=$(git rev-parse HEAD)
check "pull merges diverged branch" "0" "$(code "$sync" pull)"
check "pull result" "merged 1" "$(jq -r '"\(.status) \(.pulled)"' "$tmp/out")"
check "merge commit keeps our commit (no rebase)" "2 $ours" \
  "$(git rev-list --parents -n1 HEAD | wc -w | tr -d ' ' | awk '{print $1-1}') $(git rev-parse HEAD^1)"
check "pull again is up to date" "up-to-date" "$("$sync" pull | jq -r .status)"
check "push after merge" "0" "$(code "$sync" push)"

# conflict
cd "$tmp/b" && git pull -q --no-rebase && echo "b side" > a.txt && git commit -qam b-side && git push -q
cd "$tmp/a" && echo "a side" > a.txt && git commit -qam a-side
check "push rejected when remote moved" "4" "$(code "$sync" push)"
check "pull reports conflict" "3" "$(code "$sync" pull)"
check "conflict files listed" "conflict a.txt" "$(jq -r '"\(.status) \(.files | join(" "))"' "$tmp/out")"
check "status shows merge in progress" "merge a.txt" \
  "$("$sync" status | jq -r '"\(.in_progress) \(.conflicted | join(" "))"')"
check "pull refuses mid-merge" "5" "$(code "$sync" pull)"
check "push refuses mid-merge" "5" "$(code "$sync" push)"
git merge --abort

git checkout -q --detach
check "pull refuses detached HEAD" "5" "$(code "$sync" pull)"
check "status shows detached" "true null" "$("$sync" status | jq -r '"\(.detached) \(.branch)"')"
git checkout -q feature/x

check "no upstream pull is a no-op" "no-upstream" "$(git checkout -q -b lonely && "$sync" pull | jq -r .status)"
check "unknown command is usage error" "2" "$(code "$sync" frobnicate)"
check "push with stray arg is usage error" "2" "$(code "$sync" push origin)"
cd "$tmp" && check "outside a repo is usage error" "2" "$(code "$sync" status)"

echo
echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
