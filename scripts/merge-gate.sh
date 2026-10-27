#!/usr/bin/env bash
# merge-gate.sh — the only path by which factory merges a PR.
#
# Usage: merge-gate.sh <pr-number> <reviewed-sha>
#
# Merges only when ALL of these hold:
#   1. the PR's head is still exactly the commit the reviewer approved
#   2. the PR is mergeable and every reported check has passed
#   3. the merge policy lets a robot merge it (card kind, labels, size,
#      migrations); otherwise it is held for a human
#   4. the head, merged with the current base, passes every verify_command
#   5. (optional) Jev's risk_veto, when live, does not flag the change as
#      sensitive. Jev can only hold a merge for a human, never approve one.
# The merge itself is pinned with --match-head-commit, so a push that lands
# between the checks and the merge makes GitHub refuse it.
#
# Migrations (files matching migrations.detect) hold the PR for a human unless
# migrations.allowed is true. Then migrations.command runs AFTER GitHub
# confirms the merge, from the updated base, never before.
#
# Exit codes: 0 merged (or left for a human when auto_merge is false),
#             1 a gate failed, 2 bad usage or config,
#             4 held for a human (label factory:needs-human): merge policy,
#               migrations, or the risk veto,
#             5 merged, but the migration command failed (PR comment posted).

# shellcheck disable=SC2016  # backticks in PR comment text are Markdown, not expansions

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
mig_cmd=$(jq -r '.migrations.command // ""' "$config")

# Hold for a human: label, comment with the reasons, mark ready, exit 4.
hold() { # hold <reasons (one per line)>
  gh label create factory:needs-human --repo "$repo" --color b60205 \
    --description "Factory held this for a human decision" >/dev/null 2>&1 || true
  gh pr edit "$pr" --repo "$repo" --add-label factory:needs-human >/dev/null 2>&1 || true
  gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
  printf '🙋 **factory held this PR for a human** at `%s`.\n\n%s\n\nMerge it yourself once you are happy; factory will not merge it.\n' \
    "$head" "$(printf '%s\n' "$1" | sed 's/^/- /')" \
    | gh pr comment "$pr" --repo "$repo" --body-file - >/dev/null 2>&1 || true
  printf '%s\n' "$1" | sed 's/^/merge-gate: hold: /'
  echo "merge-gate: #$pr at $head is held for a human"
  exit 4
}

# 1. Head must still be the reviewed commit.
view=$(gh pr view "$pr" --repo "$repo" \
  --json headRefOid,headRefName,baseRefName,mergeable,isDraft,state,statusCheckRollup,labels,files,changedFiles,additions,deletions,closingIssuesReferences)
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

# 3. Merge policy. Labels come from the PR and every issue it closes, so a
#    card's kind:* label counts even when the PR itself carries none.
labels=$(jq -c '[.labels[]?.name]' <<<"$view")
for issue in $(jq -r '.closingIssuesReferences[]?.number' <<<"$view"); do
  more=$(gh issue view "$issue" --repo "$repo" --json labels --jq '[.labels[].name]' 2>/dev/null || echo '[]')
  labels=$(jq -cn --argjson a "$labels" --argjson b "$more" '$a + $b | unique')
done

# Pure function of config + PR metadata; prints "hold\t<why>" and
# "migration\t<path>" lines. Kept in a function: bash 3.2 mis-parses
# heredocs inside $(...).
policy() {
  FACTORY_PR_VIEW="$view" FACTORY_PR_LABELS="$labels" python3 - "$config" <<'PY'
import json, os, re, sys

cfg = json.load(open(sys.argv[1]))
view = json.loads(os.environ["FACTORY_PR_VIEW"])
labels = {l.lower() for l in json.loads(os.environ["FACTORY_PR_LABELS"])}
policy = cfg.get("merge_policy") or {}
mig = cfg.get("migrations") or {}

def glob(pattern):
    # ** spans directories, * and ? stay inside one path segment.
    out = re.sub(r"\\\*\\\*/|\\\*\\\*|\\\*|\\\?",
                 lambda m: {"\\*\\*/": "(?:.*/)?", "\\*\\*": ".*", "\\*": "[^/]*", "\\?": "[^/]"}[m.group()],
                 re.escape(pattern))
    return re.compile(out + r"\Z")

def hits(path, patterns):
    return any(glob(p).match(path) for p in patterns)

holds = []
files = view.get("files") or []
paths = [f.get("path") or "" for f in files]
if (view.get("changedFiles") or 0) > len(files):
    holds.append("GitHub listed %d of %d changed files; cannot check them all" % (len(files), view["changedFiles"]))

# Which kinds may merge without a human.
auto = policy.get("auto", True)
if isinstance(auto, list):  # a list names the kinds that may auto-merge
    auto = dict({k: True for k in auto}, default=False)
kinds = sorted({l.split(":", 1)[1] for l in labels if l.startswith("kind:")} |
               (labels & set(auto) if isinstance(auto, dict) else set()))
if auto is False:
    holds.append("merge_policy.auto is false: every merge needs a human")
elif isinstance(auto, dict):
    for kind in kinds or ["(no kind)"]:
        if not auto.get(kind, auto.get("default", True)):
            holds.append("kind %s is not allowed to auto-merge (merge_policy.auto)" % kind)

for label in policy.get("require_labels") or []:
    if label.lower() not in labels:
        holds.append("missing required label %s" % label)
block = policy.get("block_labels")
for label in ["do-not-merge"] if block is None else block:
    if label.lower() in labels:
        holds.append("carries blocking label %s" % label)

detect = mig.get("detect") or ["migrations/**", "**/migrations/**", "db/migrate/**",
                               "supabase/migrations/**", "prisma/migrations/**", "alembic/versions/**"]
migrations = [p for p in paths if hits(p, detect)]

# Size, leaving out lockfiles and migrations (those are judged separately).
exclude = policy.get("size_exclude") or ["**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
                                         "**/Cargo.lock", "**/poetry.lock", "**/uv.lock", "**/go.sum",
                                         "**/Gemfile.lock", "**/composer.lock", "**/pubspec.lock"]
counted = [f for f in files if f.get("path") not in migrations and not hits(f.get("path") or "", exclude)]
if files and all("additions" in f for f in files):
    lines = sum(int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in counted)
else:
    lines = int(view.get("additions") or 0) + int(view.get("deletions") or 0)
max_files = int(policy.get("max_files") or 0)  # 0 / unset = no cap
max_lines = int(policy.get("max_lines") or 0)
if max_files and len(counted) > max_files:
    holds.append("%d files changed, over merge_policy.max_files %d" % (len(counted), max_files))
if max_lines and lines > max_lines:
    holds.append("%d lines changed, over merge_policy.max_lines %d" % (lines, max_lines))

if migrations and mig.get("allowed") is not True:
    holds.append("adds migrations (%s) and migrations.allowed is not true" % ", ".join(migrations))
elif migrations and not (mig.get("command") or "").strip():
    holds.append("adds migrations (%s) but migrations.command is not set" % ", ".join(migrations))

for h in holds:
    print("hold\t" + h)
for m in migrations:
    print("migration\t" + m)
PY
}
set +e
plan=$(policy)
rc=$?
set -e
[ "$rc" -eq 0 ] || hold "merge policy could not be evaluated (exit $rc); a human decides"
reasons=$(printf '%s\n' "$plan" | sed -n 's/^hold	//p')
migrations=$(printf '%s\n' "$plan" | sed -n 's/^migration	//p')
[ -z "$reasons" ] || hold "$reasons"
[ -z "$migrations" ] || echo "merge-gate: migrations in this PR: $(printf '%s' "$migrations" | tr '\n' ' ')"

# 4. Verify head merged with the current base, in a throwaway worktree.
wt="$root/.claude/worktrees/gate-$pr-$$"
# shellcheck disable=SC2329  # invoked by the trap
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

# 5. Risk veto. Shadow mode only logs; any Jev failure lets the merge proceed
#    exactly as it would without Jev.
if [ "$(jq -r '.jev.enabled // false' "$config")" = "true" ]; then
  set +e
  verdict=$(python3 "$here/jev.py" gate "$pr")
  rc=$?
  set -e
  echo "merge-gate: risk veto: $verdict"
  [ "$rc" -ne 4 ] || hold "all gates green, but Jev's risk veto flags the change as sensitive"
fi

if [ "$auto" = "false" ]; then
  gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
  echo "merge-gate: all gates green for #$pr at $head; auto_merge is off, leaving it for a human"
  [ -z "$migrations" ] || echo "merge-gate: after merging, apply migrations with: $mig_cmd"
  exit 0
fi

gh pr ready "$pr" --repo "$repo" >/dev/null 2>&1 || true
gh pr merge "$pr" --repo "$repo" "--$method" --delete-branch --match-head-commit "$head"
echo "merge-gate: merged #$pr at $head"

# 6. Migrations, only now that the merge is real, from the updated base.
[ -n "$migrations" ] || exit 0
git -C "$root" fetch --quiet origin "$base"
git -C "$wt" checkout --quiet --force --detach "origin/$base"
echo "merge-gate: applying migrations: $mig_cmd"
log="$wt/.factory-migrate.log"
if (cd "$wt" && bash -c "$mig_cmd") >"$log" 2>&1; then
  tail -n 20 "$log"
  gh pr comment "$pr" --repo "$repo" --body "🗄 Migrations applied after merge with \`$mig_cmd\`." >/dev/null 2>&1 || true
  echo "merge-gate: migrations applied"
  exit 0
fi
tail -n 20 "$log" >&2
gh label create factory:needs-human --repo "$repo" --color b60205 \
  --description "Factory held this for a human decision" >/dev/null 2>&1 || true
gh pr edit "$pr" --repo "$repo" --add-label factory:needs-human >/dev/null 2>&1 || true
{
  printf '🚨 **MIGRATION FAILED after merge.** `%s` exited non-zero.\n\n' "$mig_cmd"
  printf 'The code is on `%s` but the database may not match it. A human needs to fix and apply:\n\n' "$base"
  printf '%s\n' "$migrations" | sed 's/^/- `/; s/$/`/'
  printf '\nLast lines of output:\n\n```\n'
  tail -n 20 "$log"
  printf '```\n'
} | gh pr comment "$pr" --repo "$repo" --body-file - >/dev/null 2>&1 || true
die "merged #$pr, but the migration command failed: $mig_cmd" 5
