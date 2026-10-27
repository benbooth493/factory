---
name: ui-refine-loop
description: Standalone check-and-fix loop that polishes one page or component with the impeccable skill. Reads the target's code, asks a few taste questions once per project, then runs bounded rounds where a fresh checker ranks typed problems and a fresh fixer applies the matching impeccable commands, with before/now screenshots each round, a fresh finish review, and a draft PR with a Before | After table. Use for /factory:ui-refine-loop, or when the user asks to refine, polish or iterate on a page's UI. Never run by the board lanes.
argument-hint: "<route | component path> [\"what's wrong or wanted\"] [--rounds N]"
---

# ui-refine-loop

A short, unattended loop over one UI surface. Each round, a **fresh checker**
finds and ranks problems, then a **fresh fixer** fixes them and lands one
green commit. Fresh contexts matter: an agent that just made the edits grades
what it meant to do, not what the pixels show. The only thing carried between
rounds is a short ledger.

```
/factory:ui-refine-loop /settings/billing "plan cards feel loud"
/factory:ui-refine-loop src/components/ReceiptList.tsx --rounds 3
```

**Standalone only.** A person starts it. The board's Build, QA and Review
lanes never run it. It works on its own branch, ends at a **draft PR**, and
never merges or pushes anywhere but that branch.

Paths: `R=${CLAUDE_PLUGIN_ROOT}/skills/ui-refine-loop/refine.sh`,
`S=${CLAUDE_PLUGIN_ROOT}/scripts/shoot.mjs`,
`C=${CLAUDE_PLUGIN_ROOT}/scripts/serve-check.sh`,
references in `${CLAUDE_PLUGIN_ROOT}/skills/ui-refine-loop/references/`.
Run scripts from the repo root unless a step says otherwise.

## 0. Required skills

Run `bash $R detect`. It prints JSON and exits 1 if a required skill is
missing. This loop needs:

- **`impeccable`**: its `critique`, `audit` and fix commands are the method.
  There is no fallback rubric.
- **`humanizer`**: for the PR prose.

If `missing` is not empty, **stop** and tell the user, then do nothing else:

> ui-refine-loop needs the `<name>` skill, which isn't installed.
> Install it (for example `npx skills add pbakaus/impeccable` and
> `npx skills add blader/humanizer`, or through `/plugin`), restart Claude
> Code, and run the command again.

Keep `impeccable` (the skill folder) and `impeccable_detect` (its detector
command, may be null) for the briefs.

## 1. Parse and read the target

Arguments: the target, an optional quoted brief, `--rounds N` (default
`ui.refine_rounds` from `.claude/factory.json`, else **3**; warn once if
over 6, since each round costs roughly 300k tokens).

Turn the target into:

- **route**: the URL path to shoot. For a component, find a page that renders
  it (grep for its import) or its Storybook story.
- **scope**: the repo-relative files and folders the fixer may edit: the page,
  the components it renders, and their styles.
- **slug**: the route or file name in kebab-case.

Then **read that code** before asking anything: the page, its components,
shared UI it uses, design tokens and theme, and how dark mode works. Write a
one-paragraph **context** for the subagents. Use an Explore subagent if it
spans many files.

## 2. Taste file (asked once per project)

The taste file is `taste_file` from detect (`ui.taste_file`, default
`.claude/factory/taste.md`). If it exists, read it and skip to step 3 unless
this target has no `### <target>` block under **Direction**; then ask only
the direction questions (1–3 below).

If it doesn't exist, ask the user **at most 10 short questions, once, in one
message**, each with your recommended answer from what you read
(`→ <recommendation>`), and say "reply 'you pick' to accept any of them".
Never ask what the code already answers. Pick from:

1. What should someone be able to do or see here that they can't now?
2. Calmer, bolder, denser, more spacious, or same look but cleaner?
3. What must not change (brand colour, layout, copy, a component)?
4. Who uses it, and how often?
5. A product or page whose feel you like?
6. Colour: add meaning, or strip it back?
7. Fonts and type scale: fixed, or open to change?
8. Motion: none, subtle, or expressive?
9. Dark mode: first-class, or just not broken?
10. Anything off-limits to the fixer (files, shared components, copy)?

Write the answers into a copy of [`references/taste.md`](references/taste.md)
(Direction block for this target; mark accepted recommendations `(picked)`).
After this the loop does not ask the user anything.

## 3. Set up

1. **Worktree.** `bash $R open <slug>` → JSON with `worktree` (`<wt>`),
   `branch`, `run` (`<run>`, a scratch dir that can't be committed),
   `base_sha`, `base_branch`. All edits happen in `<wt>`. If target files
   are uncommitted in the main checkout, tell the user they won't be in the
   worktree before you go on.
2. **Taste file in the branch.** Write it at `<wt>/<taste_file>` and commit:
   `ui-refine <slug>: taste and direction`.
3. **Dependencies and server.** Install dependencies in `<wt>` the way the
   repo does. From `<wt>`: `bash $C start`. If it exits 3, ask the user once
   for the start command and suggest saving it as `ui.start_command`. If it
   never comes up, read the log it names, fix the obvious cause once, or
   stop and tell the user.
4. **Checks.** The commands the fixer must keep green: `ui.check_commands`,
   else `verify_commands`. Run them once now; if they are already red, stop
   and say so (the loop can't tell its breakage from what was there).
5. **Round-0 shots.** From `<wt>`:

   ```bash
   node $S --base <base_url> --route <route> --out <run>/shots --label round-0 \
     --viewports refine --themes light,dark
   ```

   Desktop 1440 and mobile 390, light and dark. Add `--select` for the
   sections that matter. Read every image. A sign-in page or error overlay
   means setup isn't done.

## 4. Rounds

For round `n` = 1…N, using the Agent tool with a **new general-purpose
subagent for each role** (never reuse one, never pass one agent's transcript
to another; give each only its brief and the inputs listed there):

1. **Check.** One checker subagent with the *Checker* brief from
   [`references/briefs.md`](references/briefs.md): taste file, context,
   scope, brief, ledger, impeccable folder and detector command, and the
   latest shots (round-0 and round-`n-1`). It runs impeccable's `critique`
   and `audit` and returns at most 8 ranked, typed problems, scores, and
   `vs_before`.
2. **Stop?** Stop before fixing if the check found no P0 or P1 two checks
   in a row.
3. **Fix.** One fixer subagent with the *Fixer* brief: the problems, the
   suggested command chain from [`references/routing.md`](references/routing.md)
   (`polish` always last), taste file, context, scope, `<wt>`, the check
   commands, and the shoot command with `--label round-<n>`. It edits only
   in `<wt>`, keeps the checks green or reverts, makes **one commit**
   (`ui-refine <slug>: <what got better>`), then takes the round's shots
   (desktop 1440 and mobile 390, light and dark).
4. **Ledger.** Append one entry (format in `briefs.md`) to `<run>/ledger.md`.

Stop when the first of these happens: N fix rounds done; two checks in a row
with no P0/P1; two rounds in a row reverted; a subagent failed to return.
After the last fix, run **one closing check** so the score has a real after.

Then one **finish reviewer** subagent (the *Finish reviewer* brief) grades
every claimed fix `fixed`, `partial` or `not-fixed` from round-0 vs final
shots, and names up to three regressions.

## 5. Draft PR

1. Stop the dev server you started (`kill <pid>` from serve-check's JSON).
2. Shots onto the branch and the table:
   `bash $R table --worktree <wt> --run <run> --slug <slug> --final round-<last>`
   commits the before/after shots to `docs/factory/refine/<slug>/` and prints
   a **Before | After** table with links pinned to that commit.
3. Push the branch only: `git -C <wt> push -u origin <branch>`.
4. Write the PR body to `<run>/pr.md`:
   - **What and why**: the target and the brief, one line each.
   - **Score**: `C1 22/40 → C2 27/40 → C3 30/40`, one plain bullet per fixed
     problem.
   - **Finish review**: each fix as `- [x]` (fixed) or `- [ ]` (partial or
     not-fixed, with why), plus regressions and anything still open.
   - **Before | After**: the table.
   - **Checks**: the commands, all green, and the head SHA.
   - A note that the screenshots live in `docs/factory/refine/<slug>/` and
     that commit can be dropped before merge.
5. **Humanize.** Run the title and body through the `humanizer` skill
   (required; you checked in step 0).
6. `gh pr create --draft --base <base_branch> --head <branch> --title "<title>" --body-file <run>/pr.md`.
7. Tell the user: the PR URL, the stop reason, the score line, the grade
   counts, and where the worktree is (left in place for follow-ups; remove it
   later with `git worktree remove <wt>`).

## Rules

- Never merge, never push to the base branch, never touch the main checkout.
- A round that can't get every check green is reverted, never committed red.
- The fixer changes files outside scope only with a stated reason, and a
  shared component only behind a new prop whose default keeps today's
  behaviour.
- Refinement, not redesign: anything needing a redesign goes in the PR as an
  open question.
- No sign-in flow or data that the project doesn't provide; never invent one.
