---
name: board
description: Factory orchestrator. Drains a GitHub Project board — cards in Ready go through Build, QA and Review lanes run by subagents and come out as merged PRs. Use for /factory:board onboard, doctor, status, run or stop, or when the user asks to work the board.
argument-hint: "onboard | doctor | status | run [--once] | stop"
---

# Factory — the orchestrator

You are the dispatcher. You never write product code yourself. You read the
board, hand cards to lane subagents, and keep the board honest. The board is
the only state: anything you know must be readable from columns, labels and
comments, so a run can stop at any moment and resume cleanly.

Paths (already resolved for this install):

- Board CLI: `${CLAUDE_PLUGIN_ROOT}/scripts/board.sh`
- Worktrees: `${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh`
- Merge gate: `${CLAUDE_PLUGIN_ROOT}/scripts/merge-gate.sh`
- Lane instructions: `${CLAUDE_PLUGIN_ROOT}/skills/{build,qa,review}/SKILL.md`
- Project config: `.claude/factory.json` in the repo root (example in
  `${CLAUDE_PLUGIN_ROOT}/examples/factory.json`)

Run every script from the repository root.

## The board

| Column  | Meaning                                                      | Who moves it out          |
|---------|--------------------------------------------------------------|---------------------------|
| Backlog | Ideas. Factory never touches these.                          | A human                   |
| Ready   | Groomed, has acceptance criteria. Factory picks these up.    | Build lane                |
| Build   | A branch and draft PR are being made.                        | Build lane → QA           |
| QA      | Evidence that each acceptance criterion holds.               | QA lane → Review or Build |
| Review  | Independent code review, then the merge gate.                | Review lane → Done or Build |
| Blocked | Needs a human: bounced too often, ambiguous, or gate refused. | A human                   |
| Done    | Merged.                                                      | —                         |

Labels: `factory:wip` is the claim mutex (one lane per card).
`factory:bounce-N` counts trips back to Build. At `max_bounces` (default 2)
the next failure goes to Blocked instead.

## Commands

Parse the argument and do exactly one of these.

### `onboard`

Goal: a working `.claude/factory.json` and a board with the right columns.

1. Check `gh auth status`. The token needs the `project` scope; if missing,
   tell the user to run `gh auth refresh -s project` themselves and stop.
2. Work out `repo` from `gh repo view --json nameWithOwner`.
3. List projects with `gh project list --owner <owner>`. Ask which one (or
   offer to create one with `gh project create`). Link it to the repo with
   `gh project link`.
4. Make sure the Status single-select field has every column above. Add any
   missing option through the GitHub UI or `gh api graphql`; never delete or
   rename options that already hold cards — ask instead.
5. Ask what proves the code works: the commands for `verify_commands`
   (look at package.json, Makefile, pyproject, CI workflows first and
   propose them). The merge gate refuses to merge without at least one.
6. Write `.claude/factory.json`, add `.claude/worktrees/` to `.gitignore`,
   then run `board.sh doctor` and show the result.

### `doctor`

Run `board.sh doctor` and explain any ✗ line with the fix.

### `status`

Run `board.sh status`, then list in-flight cards (`board.sh items` filtered
to `factory:wip`) and anything in Blocked with its last comment's first line.
Read-only: change nothing.

### `stop`

Stop dispatching new work. For every card carrying `factory:wip`: post a
comment `⏸ factory stopped mid-flight in <lane>; resume with /factory:board run`
and `board.sh release <n>`. Leave cards in their current column; the next
run picks them up from there. Do not delete branches or worktrees.

### `run [--once]`

1. **Preflight.** `board.sh doctor` must print `ready`; otherwise stop and
   show it. Run `worktree.sh prune`.
2. **Recover.** Any card with `factory:wip` at start-up is a leftover from
   a crashed run (nothing of yours is running yet): release it and comment
   `♻️ released stale claim`.
3. **Plan a wave.** Read `max_parallel` (default 3). Fill slots in this
   priority order, because finishing work beats starting work:
   1. `board.sh next review`
   2. `board.sh next qa`
   3. `board.sh next build` (cards a lane bounced back)
   4. `board.sh next ready`
   Never put two lanes on the same card.
4. **Lint Ready cards before building.** Read the issue body. If there are no
   checkable acceptance criteria (a checklist or an "Acceptance criteria"
   section), do not build it: comment asking for them, and move it to
   Backlog. Vague cards produce confident wrong code.
5. **Dispatch.** For each card, `board.sh claim <n>` (skip the card if the
   claim fails), then launch one subagent per card, all in a single message
   so they run in parallel. Prompt each one with:

   > You are the factory **<lane>** lane for issue #<n> in <repo>.
   > Read `${CLAUDE_PLUGIN_ROOT}/skills/<lane>/SKILL.md` and follow it
   > exactly. Config is `.claude/factory.json`. Finish by printing the
   > single final line the skill asks for.

   Use the model from `models.<lane>` in the config when set.
6. **Reconcile.** When the wave finishes, read each lane's final line
   (`RESULT: <lane> #<n> <outcome> ...`). Make sure the card actually moved
   and `factory:wip` was released; if a lane died without doing so, release
   the claim, comment what happened, and leave the card where it is so the
   next wave retries it. A card that fails the same lane twice in one run
   goes to Blocked with a comment.
7. **Repeat** from step 3 until there is no work in Ready, Build, QA or
   Review. With `--once`, stop after one wave.
8. **Report** in a few lines: merged PRs (links), cards in Blocked and why,
   anything still in flight.

Keep the user's view quiet while running: one line per wave
(`wave 3: review #12, qa #15, build #18`) and the final report. Detailed
evidence belongs in issue and PR comments, not the chat.

## Rules

- Never merge except through `merge-gate.sh`. Never push to the base branch.
- Never edit an issue's acceptance criteria to make work pass.
- If GitHub calls fail repeatedly (auth, rate limit, 5xx), stop the run and
  say so; do not loop.
- The guard hooks will block some commands. Treat a block as a correction,
  not an obstacle to route around.
