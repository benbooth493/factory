---
name: board
description: Factory orchestrator. Drains a GitHub Project board — cards in Ready go through Build, QA and Review lanes run by subagents and come out as merged PRs. Use for /factory:board onboard, doctor, status, lint, migrate, run or stop, or when the user asks to work the board.
argument-hint: "onboard | doctor | status | lint | migrate [--dry-run] | run [--once] [--low|--high] [--codex[=<model>]] | stop"
---

# Factory board — the orchestrator

You are the dispatcher. You never write product code yourself. You read the
board, hand cards to lane subagents, and keep the board honest. The board is
the only state: anything you know must be readable from columns, labels and
comments, so a run can stop at any moment and resume cleanly.

Paths (already resolved for this install):

- Board CLI: `${CLAUDE_PLUGIN_ROOT}/scripts/board.sh`
- Worktrees: `${CLAUDE_PLUGIN_ROOT}/scripts/worktree.sh`
- Merge gate: `${CLAUDE_PLUGIN_ROOT}/scripts/merge-gate.sh`
- Jev decisions (optional): `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/jev.py`
- Model router: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/route.py` (how to use it: `${CLAUDE_PLUGIN_ROOT}/skills/board/references/router.md`)
- Codex lanes: `${CLAUDE_PLUGIN_ROOT}/scripts/codex-lane.sh` (hosts: `${CLAUDE_PLUGIN_ROOT}/docs/hosts.md`)
- Required skills check: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skills-ready.py`
- Card lint: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lint-cards.py`
- Board columns: `${CLAUDE_PLUGIN_ROOT}/scripts/board-migrate.sh`
- Writing standard: `${CLAUDE_PLUGIN_ROOT}/docs/writing-standard.md`
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
`factory:needs-human` marks a green PR the merge gate held for a person
(merge policy, migration or Jev veto); waves skip those cards until a human
merges and moves them, or removes the label.
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
4. **Columns.** Run `${CLAUDE_PLUGIN_ROOT}/scripts/board-migrate.sh --dry-run`,
   show the plan (missing columns to add, legacy columns such as Building or
   Todo to map), and on yes run it for real. It never deletes a column.
5. **Required skills.** Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skills-ready.py --all`.
   factory requires Matt Pocock's skills, impeccable and humanizer. On exit
   10, show the install commands it printed and ask the user to run them
   (plugin installs need the user); re-check afterwards. Never install
   anything yourself.
6. Ask what proves the code works: the commands for `verify_commands`
   (look at package.json, Makefile, pyproject, CI workflows first and
   propose them). The merge gate refuses to merge without at least one.
7. **Problem sources (optional).** Detect Sentry and PostHog:
   - Sentry: `@sentry/*` or `sentry-sdk` in dependencies, `sentry.properties`,
     `sentry.*.config.*` files. Org and project slugs usually live in
     `sentry.properties` or the Sentry webpack/vite plugin config.
   - PostHog: `posthog-js`, `posthog-node`, `posthog` in dependencies. The
     host is in the init call (`us.i.posthog.com` → `https://us.posthog.com`,
     `eu.i.posthog.com` → `https://eu.posthog.com`); ask for the numeric
     project id.
   Ask which to enable. Check the tokens exist with
   `${CLAUDE_PLUGIN_ROOT}/scripts/env-names.sh` (names only — never read
   `.env`): `SENTRY_AUTH_TOKEN` (scopes `event:read`, `project:read`) and
   `POSTHOG_PERSONAL_API_KEY` (scope `query:read`). If one is missing, tell
   the user where to create it and to add it to `.env` themselves. Write a
   `collect` block (shape in `${CLAUDE_PLUGIN_ROOT}/examples/factory.json`),
   then test each with `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/collect.py check <source>`
   and show the result. A failed check leaves the source enabled but
   reported as unavailable; it is not fatal to onboarding. Also offer the
   opt-in sources: `github` (adopt open issues not on the board), `prs`
   (recurring problems across merged PRs) and `architecture` (refactor
   finder); write `collect.<source>.enabled: true` for each the user wants.
8. **UI (optional).** If the app has a web UI, ask how to start it and its
   base URL and write a `ui` block (`base_url`, `start_command`) so QA can
   take screenshots; check with `${CLAUDE_PLUGIN_ROOT}/scripts/serve-check.sh`.
9. **Merge policy.** Ask which card kinds may merge without a person, and
   whether the repo has database migrations; write `merge_policy` and
   `migrations` (default: migrations held for a human).
10. **Jev decisions (optional).** If `env-names.sh` shows
   `AI_GATEWAY_API_KEY`, offer Jev (TypeSafe, via Vercel AI Gateway) for fast
   triage decisions. On yes, add a `jev` block with `enabled: true` and every
   point in `shadow` (shape in the example config), and run
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/jev.py check`. Never start a point
   in `live`; the user switches it after `jev.py report` says it's ready.
11. Write `.claude/factory.json`, add `.claude/worktrees/` and
   `.claude/factory/` to `.gitignore`, then run `board.sh doctor` and show
   the result.

### `doctor`

Run `board.sh doctor` and `skills-ready.py --all` and explain any ✗ line or
missing skill with the fix.

### `status`

Run `board.sh status`, then list in-flight cards (`board.sh items` filtered
to `factory:wip`), anything in Blocked with its last comment's first line,
and PRs labelled `factory:needs-human`. If `jev.enabled`, add a one-line
summary per decision point from `jev.py report` (mode, agreement, advice)
and this month's spend. Read-only: change nothing.

### `lint [--backlog]`

Follow `${CLAUDE_PLUGIN_ROOT}/skills/board/references/lint.md`. The flags
come from `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lint-cards.py --json`;
tightening is interactive and needs the user's approval for every edit.
Card bodies follow the card format in the writing standard and must pass
`check-writing.py card`.

### `migrate [--dry-run]`

Run `${CLAUDE_PLUGIN_ROOT}/scripts/board-migrate.sh --dry-run` and show the
plan. If the user agrees (and didn't pass `--dry-run`), run it without
`--dry-run` and show the result. It adds missing columns and moves cards out
of legacy columns (Building, In progress, Todo, In review…); it never
deletes a column.

### `stop`

Stop dispatching new work. For every card carrying `factory:wip`: post a
comment `⏸ factory stopped mid-flight in <lane>; resume with /factory:board run`
and `board.sh release <n>`. Leave cards in their current column; the next
run picks them up from there. Do not delete branches or worktrees.

### `run [--once] [--low|--high] [--codex[=<model>]]`

0. **Run flags.** `route.py args <your run arguments>` gives `host`, `tier`
   and `pin`. Keep them for the whole run and repeat them in any resume
   hint. Inside a Codex session the host is `codex` with no flag
   (`docs/hosts.md`).
1. **Preflight.** `board.sh doctor` must print `ready`, and
   `skills-ready.py --all` must exit 0; otherwise stop and show what's
   missing (for skills, the install commands it printed). Run
   `worktree.sh prune`.
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
4. **Lint Ready cards before building.** `lint-cards.py --json --issue <n>`
   decides whether the card has checkable criteria; any flag sends it to
   Backlog with the flagged lines as the comment (`/factory:board lint` is
   where the user tightens it). Vague cards produce confident wrong code.

   With `jev.enabled`, also run `jev.py decide card_lint <title+body file> --id <n>`:
   - Always record your own lint result: `jev.py record card_lint <n> pass|fail`.
   - In **live** mode (`act: true`), Jev's `label` decides pass/fail (you may
     still fail a card Jev passed if it plainly has no criteria — never the
     reverse), and its `difficulty` grades the card (below).
   - In shadow mode or when `available` is false, your judgement stands.

   **Grade the card** (easy/medium/hard) as `references/router.md` says: the
   card's own `Difficulty:` line or `difficulty:*` label, else Jev's live
   card_lint answer (`route.py card <card.json> --jev <lint.json>`), else one
   router subagent on the model `route.py router --tier <tier> --host <host> [--pin <pin>]`
   prints, using the prompt in router.md. Skip grading when that prints `none`.
5. **Dispatch.** For each card, `board.sh claim <n>` (skip the card if the
   claim fails), then launch one subagent per card, all in a single message
   so they run in parallel. Prompt each one with:

   > You are the factory **<lane>** lane for issue #<n> in <repo>.
   > Read `${CLAUDE_PLUGIN_ROOT}/skills/<lane>/SKILL.md` and follow it
   > exactly. Config is `.claude/factory.json`. Finish by printing the
   > single final line the skill asks for.

   Get the model with `route.py model --lane <lane> --grade <grade> --tier <tier> --host <host> [--pin <pin>]`;
   `inherit` means launch the subagent without a model. Cards entering past
   Ready use the `Grade:` from their Build hand-off comment, else `medium`.

   With host `codex`, do not launch subagents: run
   `bash ${CLAUDE_PLUGIN_ROOT}/scripts/codex-lane.sh <lane> <n> --model <model>`
   for each card, in parallel as background commands, and read its stdout as
   the RESULT line. Exit 3 (no RESULT) or 124 (timeout) is a lane that died:
   handle it as in step 6.
6. **Reconcile.** When the wave finishes, read each lane's final line
   (`RESULT: <lane> #<n> <outcome> ...`). Make sure the card actually moved
   and `factory:wip` was released; if a lane died without doing so, release
   the claim, comment what happened, and leave the card where it is so the
   next wave retries it. A card that fails the same lane twice in one run
   goes to Blocked with a comment.
7. **Repeat** from step 3 until there is no work in Ready, Build, QA or
   Review. With `--once`, stop after one wave.
8. **Report** in a few lines: merged PRs (links), cards in Blocked and why,
   PRs held for a human (`factory:needs-human`), anything still in flight.
   Offer `/factory:visual recap` for a merged branch if the user wants a
   picture of it.
9. **Calibrate (optional).** Follow
   `${CLAUDE_PLUGIN_ROOT}/skills/board/references/calibration.md`; it skips
   silently unless configured.

Keep the user's view quiet while running: one line per wave
(`wave 3: review #12, qa #15, build #18 (hard → session)`) and the final report. Detailed
evidence belongs in issue and PR comments, not the chat.

## Rules

- Never merge except through `merge-gate.sh`. Never push to the base branch.
- Never merge or unlabel a PR held with `factory:needs-human`; that's the
  human's call.
- Never edit an issue's acceptance criteria to make work pass.
- If GitHub calls fail repeatedly (auth, rate limit, 5xx), stop the run and
  say so; do not loop.
- The guard hooks will block some commands. Treat a block as a correction,
  not an obstacle to route around.
