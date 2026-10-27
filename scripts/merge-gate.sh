#!/usr/bin/env bash
# merge-gate.sh — the only path by which factory merges a PR.
#
# Usage: merge-gate.sh <pr-number> <reviewed-sha>
#
# Merges only when ALL of these hold:
#   1. the PR's head is still exactly the commit the reviewer approved
#   2. the PR is mergeable and every reported check has passed
#   3. the head, merged with the current base, passes every verify_command
#   4. (optional) Jev's risk_veto, when live, does not flag the change as
#      sensitive. Jev can only hold a merge for a human, never approve one.
# The merge itself is pinned with --match-head-commit, so a push that lands
# between the checks and the merge makes GitHub refuse it.
#
# Exit codes: 0 merged (or left for a human when auto_merge is false),
#             1 a gate failed, 2 bad usage or config,
#             4 held for a human by the risk veto (label factory:needs-human).

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
die() { echo "merge-gate: $*" >&2; exit "${2:-1}"; }

[ $# -eq 2 ] || die "usage: merge-gate.sh <pr-number> <reviewed-sha>" 2
pr="$1"; reviewed="$2"

root=$(git rev-parse --show-toplevel)
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"
[ -f "$config" ] || die "no config at $config" 2

repo=$(jq -r .repo "$config")
base=$(jq -r '.base_branch // "main"' "$config")
method=$(jq -r '.merge_method // "squash"' "$config")
auto=$(jq -r 'if .auto_merge == false then "false" else "true" end' "$config")
ncmds=$(jq '.verify_commands | length' "$config")
[ "$ncmds" -gt 0 ] || die "verify_commands is empty; refusing to merge unverified code" 2
case "$method" in squash|merge|rebase) ;; *) die "merge_method must be squash, merge or rebase" 2 ;; esac

# 1. Head must still be the reviewed commit.
view=$(gh pr view "$pr" --repo "$repo" \
  --json headRefOid,headRefName,baseRefName,mergeable,isDraft,state,statusCheckRollup)
head=$(jq -r .headRefOid <<<"$view")
[ "$(jq -r .state <<<"$view")" = "OPEN" ] || die "PR #$pr is not open"
[ "$head" = "$reviewed" ] || die "PR #$pr head is $head but review approved $reviewed; re-review needed"
[ "$(jq -r .baseRefName <<<"$view")" = "$base" ] || die "PR #$pr targets $(jq -r .baseRefName <<<"$view"), not $base"

# 2. Mergeable, and no failing or pending checks.
[ "$(jq -r .mergeable <<<"$view")" != "CONFLICTING" ] || die "PR #$pr has merge conflicts"
bad=$(jq -r '[.statusCheckRollup[]?
  | (.conclusion // .state // "")
  | ascii_upcase
  | select(. != "SUCCESS" and . != "NEUTRAL" and . != "SKIPPED")] | length' <<<"$view")
[ "$bad" -eq 0 ] || die "PR #$pr has $bad check(s) not passing yet"

# 3. Verify head merged with the current base, in a throwaway worktree.
wt="$root/.claude/worktrees/gate-$pr-$$"
cleanup() { git -C "$root" worktree remove --force "$wt" >/dev/null 2>&1 || true; }
trap cleanup EXIT

git -C "$root" fetch --quiet origin "$base" "$head"
git -C "$root" worktree add --quiet --detach "$wt" "$head"
git -C "$wt" -c user.name=factory -c user.email=factory@localhost \
  merge --quiet --no-edit "origin/$base" >/dev/null || die "PR #$pr does not merge cleanly with $base"

i=0
while [ "$i" -lt "$ncmds" ]; do
  cmd=$(jq -r ".verify_commands[$i]" "$config")
  echo "merge-gate: running: $cmd"
  (cd "$wt" && bash -c "$cmd") || die "verify command failed: $cmd"
  i=$((i + 1))
done

# 4. Risk veto. Shadow mode only logs; any Jev failure lets the merge proceed
#    exactly as it would without Jev.
if [ "$(jq -r '.jev.enabled // false' "$config")" = "true" ]; then
  set +e
  verdict=$(python3 "$here/jev.py" gate "$pr")
  rc=$?
  set -e
  echo "merge-gate: risk veto: $verdict"
  if [ "$rc" -eq 4 ]; then
    gh label create factory:needs-human --repo "$repo" --color b60205 \
      --description "Factory held this for a human decision" >/dev/null 2>&1 || true
    gh pr edit "$pr" --repo "$repo" --add-label factory:needs-human >/dev/null 2>&1 || true
    gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
    echo "merge-gate: all gates green for #$pr at $head, but the change looks sensitive; held for a human"
    exit 4
  fi
fi

if [ "$auto" = "false" ]; then
  gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
  echo "merge-gate: all gates green for #$pr at $head; auto_merge is off, leaving it for a human"
  exit 0
fi

gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
gh pr merge "$pr" --repo "$repo" "--$method" --delete-branch --match-head-commit "$head"
echo "merge-gate: merged #$pr at $head"
