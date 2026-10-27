#!/usr/bin/env bash
# git-sync.sh — the mechanical, testable parts of /factory:git-sync.
#
# Usage (from inside the repo):
#   git-sync.sh status [--fetch]   JSON summary of the branch and working tree
#   git-sync.sh pull               merge-pull the upstream (never rebase)
#   git-sync.sh push               push without force; refuses protected branches
#   git-sync.sh branch <name>      move work (and unpushed commits) off a
#                                  protected branch onto a new branch
#
# Protected branches: main, master, base_branch and protected_branches from
# .claude/factory.json, and the remote's default branch.
#
# Exit codes: 0 ok · 1 git failed · 2 usage · 3 merge conflict
#             4 push rejected (pull first) · 5 refused (force, protected,
#             detached HEAD, operation in progress)

set -euo pipefail

die() { echo "git-sync: $*" >&2; exit "${2:-1}"; }
refuse() { echo "git-sync: refused: $*" >&2; exit 5; }

root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository" 2
cd "$root"
gitdir=$(git rev-parse --git-dir)
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"
plugin_root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

nul_list() { jq -Rs 'split("\u0000") | map(select(length > 0))'; }

branch() { git symbolic-ref --quiet --short HEAD 2>/dev/null || true; }

upstream() { git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true; }

in_progress() {
  if [ -f "$gitdir/MERGE_HEAD" ]; then echo merge
  elif [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then echo rebase
  elif [ -f "$gitdir/CHERRY_PICK_HEAD" ]; then echo cherry-pick
  elif [ -f "$gitdir/REVERT_HEAD" ]; then echo revert
  fi
}

protected_list() {
  echo main; echo master
  if [ -f "$config" ]; then
    jq -r '(.base_branch // empty), (.protected_branches // [] | .[])' "$config" 2>/dev/null || true
  fi
  git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##' || true
}

is_protected() { protected_list | grep -qxF -- "$1"; }

# Changed paths that are secrets by name, or that contain a live-looking key.
# Reuses the guard hooks' own rules so git-sync and the guards agree.
SECRET_PY='
import os, sys
sys.path.insert(0, sys.argv[1])
from guard_bash import is_secret
from guard_files import find_key
for path in filter(None, sys.stdin.read().split("\0")):
    if is_secret(path):
        print(path + "\0" + "secret file", end="\0")
        continue
    try:
        if os.path.isfile(path) and os.path.getsize(path) < 1000000:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                found = find_key(fh.read())
            if found:
                print(path + "\0" + found, end="\0")
    except OSError:
        pass
'
secret_paths() { python3 -c "$SECRET_PY" "$plugin_root/hooks"; }

cmd_status() {
  local fetch=0 b up ahead=0 behind=0 detached=false prot=false fetch_error=""
  [ "${1:-}" = "--fetch" ] && fetch=1
  b=$(branch)
  [ -z "$b" ] && detached=true
  up=$(upstream)
  if [ -n "$up" ] && [ "$fetch" = 1 ]; then
    git fetch --quiet "${up%%/*}" 2>/dev/null || fetch_error="fetch from ${up%%/*} failed"
  fi
  if [ -n "$up" ]; then
    read -r ahead behind < <(git rev-list --left-right --count "HEAD...$up" 2>/dev/null || echo "0 0")
  fi
  if [ -n "$b" ] && is_protected "$b"; then prot=true; fi

  local staged unstaged untracked conflicted secrets
  staged=$(git diff --cached --name-only -z | nul_list)
  unstaged=$(git diff --name-only -z | nul_list)
  untracked=$(git ls-files --others --exclude-standard -z | nul_list)
  conflicted=$(git diff --name-only --diff-filter=U -z | nul_list)
  secrets=$({ git diff --cached --name-only -z; git diff --name-only -z
              git ls-files --others --exclude-standard -z; } | secret_paths \
            | jq -Rs 'split("\u0000") | map(select(length > 0))
                      | [range(0; length; 2) as $i | {path: .[$i], why: .[$i + 1]}]
                      | unique_by(.path)')

  jq -n --arg branch "$b" --argjson detached "$detached" --arg upstream "$up" \
    --argjson ahead "$ahead" --argjson behind "$behind" --arg op "$(in_progress)" \
    --argjson protected "$prot" --arg fetch_error "$fetch_error" \
    --argjson staged "$staged" --argjson unstaged "$unstaged" \
    --argjson untracked "$untracked" --argjson conflicted "$conflicted" \
    --argjson secrets "$secrets" '
    {branch: (if $branch == "" then null else $branch end), detached: $detached,
     upstream: (if $upstream == "" then null else $upstream end),
     ahead: $ahead, behind: $behind,
     in_progress: (if $op == "" then null else $op end),
     protected: $protected,
     staged: $staged, unstaged: $unstaged, untracked: $untracked,
     conflicted: $conflicted, secrets: $secrets,
     clean: (($staged + $unstaged + $untracked) | length == 0),
     in_sync: ((($staged + $unstaged + $untracked) | length == 0) and $ahead == 0 and $behind == 0)}
    + (if $fetch_error == "" then {} else {fetch_error: $fetch_error} end)'
}

preflight() {
  [ -n "$(branch)" ] || refuse "detached HEAD; check out a branch first"
  local op
  op=$(in_progress)
  [ -z "$op" ] || refuse "a $op is in progress; finish or abort it first"
}

cmd_pull() {
  preflight
  local up behind out
  up=$(upstream)
  if [ -z "$up" ]; then echo '{"status":"no-upstream","pulled":0}'; return 0; fi
  git fetch --quiet "${up%%/*}" || die "fetch from ${up%%/*} failed"
  behind=$(git rev-list --count "HEAD..$up")
  if [ "$behind" = 0 ]; then echo '{"status":"up-to-date","pulled":0}'; return 0; fi
  # --ff overrides a pull.ff=only setting so a diverged branch merges instead of failing.
  if out=$(git pull --quiet --no-rebase --ff --no-edit 2>&1); then
    jq -n --argjson n "$behind" '{status: "merged", pulled: $n}'
    return 0
  fi
  if [ -f "$gitdir/MERGE_HEAD" ] && [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    git diff --name-only --diff-filter=U -z | nul_list \
      | jq --argjson n "$behind" '{status: "conflict", pulled: $n, files: .}'
    exit 3
  fi
  echo "$out" >&2
  die "pull failed"
}

cmd_push() {
  local a
  for a in "$@"; do
    case "$a" in
      -f|--force|--force-with-lease*|--force-if-includes|--mirror|+*)
        refuse "force push ($a); git-sync never forces. Pull and merge instead" ;;
      -d|--delete|:*) refuse "deleting remote branches ($a)" ;;
      *) die "push takes no arguments (got $a)" 2 ;;
    esac
  done
  preflight
  local b up dest out
  b=$(branch)
  up=$(upstream)
  dest="${up#*/}"
  is_protected "$b" && refuse "on protected branch $b; move the work to a branch: git-sync.sh branch <name>"
  [ -n "$up" ] && is_protected "$dest" && refuse "$b tracks protected $up; push to a branch of its own"
  if [ -n "$up" ]; then
    out=$(git push --quiet "${up%%/*}" "HEAD:refs/heads/$dest" 2>&1) || {
      echo "$out" >&2
      if echo "$out" | grep -qiE 'non-fast-forward|fetch first|rejected'; then
        echo "git-sync: push rejected; run git-sync.sh pull, then push again" >&2; exit 4
      fi
      die "push failed"
    }
  else
    up="origin/$b"
    git push --quiet -u origin "$b" >&2 || die "push failed"
  fi
  jq -n --arg b "$b" --arg up "$up" '{status: "pushed", branch: $b, upstream: $up}'
}

cmd_branch() {
  local new="${1:-}" old up
  [ -n "$new" ] || die "usage: git-sync.sh branch <name>" 2
  preflight
  is_protected "$new" && refuse "$new is itself protected"
  git check-ref-format --branch "$new" >/dev/null 2>&1 || die "bad branch name: $new" 2
  old=$(branch)
  up=$(upstream)
  git checkout --quiet -b "$new"   # uncommitted changes come along
  # Unpushed commits now live on $new; put the protected branch back where
  # its upstream is so nothing reaches it except through a PR.
  if is_protected "$old" && [ -n "$up" ]; then
    git branch --quiet -f "$old" "$up"
  fi
  jq -n --arg new "$new" --arg old "$old" '{status: "moved", branch: $new, from: $old}'
}

[ $# -ge 1 ] || die "usage: git-sync.sh status [--fetch] | pull | push | branch <name>" 2
cmd="$1"; shift
case "$cmd" in
  status) cmd_status "$@" ;;
  pull) cmd_pull ;;
  push) cmd_push "$@" ;;
  branch) cmd_branch "$@" ;;
  *) die "unknown command: $cmd" 2 ;;
esac
