#!/usr/bin/env bash
# worktree.sh — one git worktree per card, always under .claude/worktrees/.
#
# Usage:
#   worktree.sh path <issue>      print the worktree path for a card
#   worktree.sh open <issue>      create (or reuse) branch factory/<issue> + worktree
#   worktree.sh close <issue>     remove the worktree (branch kept)
#   worktree.sh prune             remove worktrees whose branch is merged or gone
#
# Branch names are factory/<issue>-<slug> when created with a title via
# FACTORY_TITLE, otherwise factory/<issue>. An existing remote branch for the
# issue is reused so a bounced card continues on the same PR.

set -euo pipefail

die() { echo "worktree: $*" >&2; exit 1; }

root=$(git rev-parse --show-toplevel)
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"
base=$(jq -r '.base_branch // "main"' "$config" 2>/dev/null || echo main)
wt_root="$root/.claude/worktrees"

slug() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
    | sed -e 's/^-//' -e 's/-$//' | cut -c1-40 | sed -e 's/-$//'
}

wt_path() { echo "$wt_root/issue-$1"; }

existing_branch() {
  # Remote or local branch already made for this issue, if any.
  git -C "$root" for-each-ref --format='%(refname:short)' \
    "refs/heads/factory/$1" "refs/heads/factory/$1-*" \
    "refs/remotes/origin/factory/$1" "refs/remotes/origin/factory/$1-*" \
    | sed 's#^origin/##' | head -n1
}

cmd_open() {
  local issue="$1" path branch
  path=$(wt_path "$issue")
  if [ -d "$path" ]; then
    git -C "$path" pull --quiet --ff-only 2>/dev/null || true
    echo "$path"; return
  fi
  mkdir -p "$wt_root"
  git -C "$root" fetch --quiet origin "$base" || true
  git -C "$root" fetch --quiet origin "refs/heads/factory/*:refs/remotes/origin/factory/*" 2>/dev/null || true
  branch=$(existing_branch "$issue")
  if [ -n "$branch" ]; then
    if git -C "$root" show-ref --quiet "refs/heads/$branch"; then
      git -C "$root" worktree add --quiet "$path" "$branch" >&2
    else
      git -C "$root" worktree add --quiet -b "$branch" "$path" "origin/$branch" >&2
    fi
  else
    if [ -n "${FACTORY_TITLE:-}" ]; then
      branch="factory/$issue-$(slug "$FACTORY_TITLE")"
    else
      branch="factory/$issue"
    fi
    git -C "$root" worktree add --quiet -b "$branch" "$path" "origin/$base" >&2
  fi
  echo "$path"
}

cmd_close() {
  local path
  path=$(wt_path "$1")
  [ -d "$path" ] || return 0
  git -C "$root" worktree remove --force "$path"
}

cmd_prune() {
  # Only remove a worktree once its PR is merged or closed. A branch with no
  # PR yet may be a Build lane mid-flight, so it is always left alone.
  local path branch state repo
  repo=$(jq -r .repo "$config")
  for path in "$wt_root"/issue-*; do
    [ -d "$path" ] || continue
    branch=$(git -C "$path" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    [ -n "$branch" ] && [ "$branch" != "HEAD" ] || continue
    state=$(gh pr view "$branch" --repo "$repo" --json state --jq .state 2>/dev/null || echo NONE)
    if [ "$state" = "MERGED" ] || [ "$state" = "CLOSED" ]; then
      echo "pruning $path ($branch, PR $state)"
      git -C "$root" worktree remove --force "$path" || true
      [ -n "$branch" ] && git -C "$root" branch -D "$branch" >/dev/null 2>&1 || true
    fi
  done
  git -C "$root" worktree prune
}

[ $# -ge 1 ] || die "usage: worktree.sh path|open|close <issue> | prune"
case "$1" in
  path) [ $# -eq 2 ] || die "usage: path <issue>"; wt_path "$2" ;;
  open) [ $# -eq 2 ] || die "usage: open <issue>"; cmd_open "$2" ;;
  close) [ $# -eq 2 ] || die "usage: close <issue>"; cmd_close "$2" ;;
  prune) cmd_prune ;;
  *) die "unknown command: $1" ;;
esac
