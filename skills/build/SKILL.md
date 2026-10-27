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

## Skills

Required packs (factory halts without them; see `/factory:board doctor`):
check with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skills-ready.py --lane build --kind <kind>`
where kind comes from the card's `bug` / `feature` / `refactor` label (default
`feature`). Exit 10 means a skill is missing: comment which one on the
issue, `$B release <n>`, and finish with `RESULT: build #<n> blocked reason="missing skill"`.

Load and follow, by kind:

- every card: `mattpocock-skills:tdd`
- `bug`: `mattpocock-skills:diagnosing-bugs`
- `feature`: `mattpocock-skills:implement`, `mattpocock-skills:codebase-design`
- `refactor`: `mattpocock-skills:codebase-design`
- before the last commit: `mattpocock-skills:code-review` on your own diff
- merge conflicts with the base: `mattpocock-skills:resolving-merge-conflicts`
- simplest solution first: `ponytail:ponytail` if installed; otherwise ask,
  before writing code, whether the change is needed at all, whether the
  codebase already has it, and what the smallest version is.

A `Skills:` line in the issue body (`Skills: a, b`) replaces the kind
defaults, but `tdd` and `code-review` always apply. Never load an interactive
skill (one that waits for the user) inside a lane.

## Steps

1. **Move the card.** `$B move <n> build`.
2. **Read everything first.** `gh issue view <n> --comments`. If a PR already
   exists for this issue (a bounce), read its review and QA comments too:
   `gh pr list --search "<n> in:body" --state open`. Bounce feedback is your
   to-do list; address every point. A `bounce:<reason>` label on the issue
   says what kind of failure it was: start there (for `missing_test`, write
   the test before touching the code; for `criteria_misread`, re-read the
   criteria and say in the PR how you now read each one).
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
8. **Commit and push.** One or a few commits, one per logical change, with
   messages written to `${CLAUDE_PLUGIN_ROOT}/docs/writing-standard.md`. Save
   each message to a file in your scratchpad, run
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py commit <file>`, fix
   what it reports, then `git commit -F <file>`.
   `git push -u origin HEAD` (use `--force-with-lease` only on your own
   `factory/*` branch, only after a rebase).
9. **Open or update the PR.** New: `gh pr create --draft --base <base_branch>`
   with a title and body in the PR format of
   `${CLAUDE_PLUGIN_ROOT}/docs/writing-standard.md` (`Closes #<n>`, `## Why`,
   `## What changed`, `## Acceptance criteria` mapping each criterion to the
   file(s) and test(s) that cover it, `## Risk` when it applies). Run the
   body through the `humanizer` skill, then
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py pr <body-file> --title "<title>"`
   and fix what it reports before `gh pr create --body-file <body-file>`. On a bounce, push to the same branch and comment on the PR
   with what changed per feedback point.
10. **Hand off.** Comment on the issue:

    ```
    🔨 Build done → QA
    PR: #<pr>  head: <short sha>
    Grade: <easy|medium|hard>
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
