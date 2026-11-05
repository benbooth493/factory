#!/usr/bin/env bash
# evidence.sh — keep QA screenshots out of project branches.
#
# Usage:
#   evidence.sh push <issue> <tested sha> <shots dir>
#
# Commits every file in <shots dir> to runs/<issue>/qa-<short sha>/ on the
# orphan branch factory-evidence (created on first use) and pushes it. It
# never checks anything out, so the worktree and the PR branch are untouched.
# A rejected push (another QA lane got there first) fetches the new tip,
# rebuilds the commit on top of it and retries; it never forces.
#
# Prints JSON: {"sha": evidence commit, "dir": path on the branch,
# "url": "https://github.com/<repo>/blob/<sha>/<dir>"} — append "/<file>?raw=true".

set -euo pipefail

die() { echo "evidence: $*" >&2; exit 1; }

branch=factory-evidence
root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"

cmd_push() {
  [ $# -eq 3 ] || die "usage: evidence.sh push <issue> <tested sha> <shots dir>"
  local issue="$1" short src="$3" dir repo files=() f parent tree commit attempt
  short=$(git rev-parse --short "$2^{commit}") || die "unknown commit $2"
  [ -d "$src" ] || die "no such directory: $src"
  for f in "$src"/*; do if [ -f "$f" ]; then files+=("$f"); fi; done
  [ ${#files[@]} -gt 0 ] || die "no files in $src"
  dir="runs/$issue/qa-$short"
  repo=$(jq -r '.repo // empty' "$config" 2>/dev/null || true)
  [ -n "$repo" ] || die "no repo in $config"

  idx=$(mktemp)
  trap 'rm -f "$idx"' EXIT
  for attempt in 1 2 3 4 5; do
    parent=""
    if git fetch --quiet origin "+refs/heads/$branch:refs/remotes/origin/$branch" 2>/dev/null; then
      parent=$(git rev-parse "refs/remotes/origin/$branch")
    fi
    rm -f "$idx"
    if [ -n "$parent" ]; then GIT_INDEX_FILE="$idx" git read-tree "$parent"
    else GIT_INDEX_FILE="$idx" git read-tree --empty; fi
    for f in "${files[@]}"; do
      GIT_INDEX_FILE="$idx" git update-index --add --cacheinfo \
        "100644,$(git hash-object -w "$f"),$dir/$(basename "$f")"
    done
    tree=$(GIT_INDEX_FILE="$idx" git write-tree)
    commit=$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "QA evidence for #$issue at $short")
    if git push --quiet origin "$commit:refs/heads/$branch" 2>/dev/null; then
      jq -n --arg sha "$commit" --arg dir "$dir" --arg url "https://github.com/$repo/blob/$commit/$dir" \
        '{sha: $sha, dir: $dir, url: $url}'
      return
    fi
    echo "evidence: push rejected (attempt $attempt), refetching $branch" >&2
    sleep "$attempt"
  done
  die "could not push to $branch after 5 attempts"
}

case "${1:-}" in
  push) shift; cmd_push "$@" ;;
  *) die "usage: evidence.sh push <issue> <tested sha> <shots dir>" ;;
esac
