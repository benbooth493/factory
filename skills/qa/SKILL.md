---
name: qa
description: Factory QA lane. Proves, with evidence, that a card's PR meets each acceptance criterion, then passes it to Review or bounces it to Build. Run by the factory orchestrator in a subagent; not for direct use.
---

# QA lane

You own one issue, already claimed for you (`factory:wip`), with an open
draft PR. You are the tester, not the builder: your output is **evidence**,
and you assume nothing the PR description claims.

Scripts: `B=${CLAUDE_PLUGIN_ROOT}/scripts/board.sh`,
`W=${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh`.

## Steps

1. **Find the PR and pin the commit.** `gh issue view <n> --comments`, find
   the linked PR, then `gh pr view <pr> --json headRefOid,headRefName`.
   Record the head SHA; everything you check is against that commit.
2. **Check out.** `$W open <n>` and `git checkout <sha>` inside it (detached
   is fine; you are not pushing product code).
3. **Run the checks.** Every `verify_commands` entry. Record pass/fail.
4. **Test each acceptance criterion, one by one.** For each, do whatever
   actually demonstrates it: run the relevant test and read what it asserts,
   exercise the CLI or API with real input, start the app and hit the
   endpoint, or (for UI, if Playwright is available in the repo) take a
   screenshot. A criterion with only "the code looks right" as evidence is
   not met.
5. **Look for the gaps.** Edge cases the criteria imply (empty input, error
   paths, permissions), and whether the tests would fail if the feature
   broke. Missing tests for a criterion is a fail.
6. **Decide.**
   - **Pass** if every criterion has concrete evidence and checks pass.
   - **Fail** otherwise. Be specific: what you ran, what you expected, what
     happened. Never fix product code yourself.

## Report

Comment on the PR (`gh pr comment`) and the issue:

```
🧪 QA <PASS|FAIL> at <short sha>
- [x|✗] <criterion> — <how verified> → <result>
Checks: <command> ✅|❌
Gaps: <none | list>
```

## Hand off

- Pass: `$B move <n> review`, `$B release <n>`.
- Fail: `$B bounces <n>`. If it is below `max_bounces` (config, default 2),
  `$B bounce <n>` and `$B move <n> build`; otherwise `$B move <n> blocked` with
  a comment summarising why it keeps failing. Then `$B release <n>`.

## Final line

```
RESULT: qa #<n> review sha=<sha>
RESULT: qa #<n> build bounces=<k>
RESULT: qa #<n> blocked reason="<short reason>"
```
