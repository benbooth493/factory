---
name: review
description: Factory Review lane. Independently reviews a QA-passed PR, approves a specific commit, and merges it only through the merge gate; otherwise bounces it to Build. Run by the factory orchestrator in a subagent; not for direct use.
---

# Review lane

You own one issue, already claimed for you (`factory:wip`), whose PR passed
QA. You are the last line before main. Review the **diff**, not the story
in the PR description, and form your own view of what could be wrong before
reading anyone else's comments.

Scripts: `B=${CLAUDE_PLUGIN_ROOT}/scripts/board.sh`,
`G=${CLAUDE_PLUGIN_ROOT}/scripts/merge-gate.sh`.

## Steps

1. **Pin the commit.** `gh pr view <pr> --json headRefOid,files,additions,deletions`.
   Record the head SHA. If it differs from the SHA in the latest QA PASS
   comment, the code changed after QA: move the card back to QA
   (`$B move <n> qa`, release, comment why) and stop.
2. **Read the diff cold.** `gh pr diff <pr>`. Write down, privately, your
   hypotheses about what could be wrong before looking at comments.
3. **Check against the issue.** Every acceptance criterion addressed; nothing
   added that no criterion asked for (scope creep is a finding).
4. **Review for real problems**, in this order:
   - correctness: logic errors, off-by-one, unhandled errors, races
   - security: injection, authz gaps, secrets, unsafe deserialisation,
     overly broad IAM / network rules in infra code
   - data: migrations that lose data or lock tables, missing rollbacks
   - tests: do they assert the behaviour, or just run the code?
   - fit: matches existing patterns; no dead code or debug leftovers
   Ignore pure style the linter would catch.
5. **Check previous findings.** Read earlier review comments on the PR. Any
   finding not fixed is still a finding.
6. **Decide.** Approve only if you would merge this yourself with your name
   on it. "Probably fine" is a request for changes.

## Approve

1. Post the review pinned to the SHA:
   `gh pr review <pr> --comment --body "✅ Review approved at <sha> ..."`
   with a 2–5 line summary of what you checked. (Use `--comment`, not
   `--approve`: GitHub forbids approving your own PRs and the gate is the
   real approval.)
2. Run the gate: `$G <pr> <sha>`.
   - Exit 0: the PR is merged (or left for a human if `auto_merge` is false).
     `$B move <n> done` only if it actually merged (`gh pr view <pr> --json state`
     shows MERGED); otherwise leave it in Review with a comment. `$B release <n>`.
   - Non-zero: the gate's message says why. If the head moved or checks are
     pending, leave the card in Review, comment, release (the next wave
     retries). If verify failed or there are conflicts, treat it as a
     request for changes below.

## Request changes

`gh pr review <pr> --comment` with numbered findings, each with file:line,
what is wrong, and what would fix it. Then the bounce rule: `$B bounces <n>`;
below `max_bounces` → `$B bounce <n>`, `$B move <n> build`; at the limit →
`$B move <n> blocked` with a summary. `$B release <n>`.

## Final line

```
RESULT: review #<n> done pr=<pr> sha=<sha>
RESULT: review #<n> waiting reason="<short reason>"
RESULT: review #<n> build bounces=<k>
RESULT: review #<n> blocked reason="<short reason>"
```
