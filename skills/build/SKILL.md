---
name: build
description: Factory Build lane. Turns one claimed card into a branch and a draft PR with passing checks, then hands it to QA. Run by the factory orchestrator in a subagent; not for direct use.
---

# Build lane

You own exactly one issue, already claimed for you (`factory:wip`). Your job:
the **smallest change that meets every acceptance criterion**, on a branch,
in a draft PR, with the project's checks passing.

Scripts: `B=${CLAUDE_PLUGIN_ROOT}/scripts/board.sh`,
`W=${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh`. Run them from the repo root.

## Steps

1. **Move the card.** `$B move <n> build`.
2. **Read everything first.** `gh issue view <n> --comments`. If a PR already
   exists for this issue (a bounce), read its review and QA comments too:
   `gh pr list --search "<n> in:body" --state open`. Bounce feedback is your
   to-do list; address every point.
3. **Get a worktree.** `FACTORY_TITLE="<issue title>" $W open <n>` prints the
   path. Do all work there. Read the repo's `CLAUDE.md` / `AGENTS.md` /
   `CONTRIBUTING.md` if present and follow them.
4. **Understand before editing.** Find the code the criteria touch. Note the
   existing patterns, test style and naming, and copy them.
5. **Write the test first** for each criterion that can be tested, and see
   it fail for the right reason.
6. **Make the change.** Smallest diff that satisfies the criteria. No drive-by
   refactors, no new dependencies unless a criterion needs one, no edits to
   unrelated files. If a criterion is ambiguous or contradicts the code, stop
   and go to *Blocked* below rather than guessing.
7. **Verify.** Run every command in `verify_commands` from the config inside
   the worktree. All must pass. Fix and re-run; do not skip or weaken tests.
8. **Commit and push.** One or a few commits with messages that say why.
   `git push -u origin HEAD` (use `--force-with-lease` only on your own
   `factory/*` branch, only after a rebase).
9. **Open or update the PR.** New: `gh pr create --draft --base <base_branch>`
   with a body containing `Closes #<n>`, a short summary of the approach, and
   a checklist mapping each acceptance criterion to the file(s) and test(s)
   that cover it. On a bounce, push to the same branch and comment on the PR
   with what changed per feedback point.
10. **Hand off.** Comment on the issue:

    ```
    🔨 Build done → QA
    PR: #<pr>  head: <short sha>
    Criteria → evidence:
    - [x] <criterion>: <test or file>
    Verify: <commands> ✅
    ```

    Then `$B move <n> qa` and `$B release <n>`.

## Blocked

If you cannot proceed (ambiguous criteria, needs a secret or access you
lack, the change is far bigger than the card implies), comment on the issue
with the specific question, `$B move <n> blocked`, `$B release <n>`.
Push any useful partial work to the branch first.

## Final line

Print exactly one of these as your last line:

```
RESULT: build #<n> qa pr=<pr> sha=<sha>
RESULT: build #<n> blocked reason="<short reason>"
```
