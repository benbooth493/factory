# Subagent briefs

Three roles, each a **new** subagent every time it runs. Paste the role's
section into its prompt with the inputs listed. Nobody asks the user
anything; questions go in `open_questions`.

Every role reads the **taste file** first. Where it and a generic playbook
disagree, the taste file wins. Cite its rule ids (T1, S4) in evidence.

---

## Checker

Inputs: taste file, context paragraph, scope paths, the user's brief, the
ledger so far, the impeccable skill folder and detector command, the
round-0 shots and the latest shots, and the shoot command (in case you need
another state).

You look; you don't edit, commit or push.

1. **Critique.** Follow impeccable's `critique` from its skill folder for
   the visual review only. Skip any steps that ask the user something or
   open a live overlay. Look at every shot: light and dark, 1440 and 390.
   Then read the source files in scope. Score 0–4 each: `specificity`,
   `hierarchy`, `typography`, `color`, `composition` → `design` /20.
2. **Audit.** Follow impeccable's `audit`. If the detector command is set,
   run it over the scope paths from the worktree and check each hit in the
   source (drop false positives). Score 0–4 each: `accessibility`,
   `performance`, `responsive`, `theming`, `integrity` → `audit` /20.
   If the detector is missing or crashes, say so; don't pretend it ran.
3. **Merge into at most 8 problems**, ranked:
   - **P0** broken: the task can't be done, content clipped or unreadable,
     a theme unusable.
   - **P1** blocks the brief or the page's job, or fails accessibility.
   - **P2** off-taste or sloppy, but the job gets done.
   - **P3** polish.

   Drop anything the brief or taste file says to keep. Reuse an id from the
   ledger when it's the same issue; if an id has survived three checks, say
   so.
4. **Before vs now.** Compare round-0 with the latest shots: `better`,
   `same` or `worse` (`baseline` on the first check), plus one line by
   section. A section that got worse is a problem of its own.

Each problem:

```json
{"id": "summary-strip-cramped", "severity": "P1", "type": "spacing-hierarchy",
 "location": "src/app/reports/Summary.tsx:42",
 "evidence": "round-2-mobile-dark.png: figures wrap under labels (T4)",
 "fix": "figures on one line at 390, labels above, existing spacing tokens"}
```

`type` is exactly one row of [`routing.md`](routing.md), chosen for the
cause, not the symptom. No `location`, no problem: find the line. Never type
something as a redesign; put that in `open_questions`.

Return JSON: `design`, `audit`, `problems`, `vs_before`, `vs_before_note`,
`summary` (two sentences), `open_questions`.

---

## Fixer

Inputs: the checker's problems, the suggested command chain, taste file,
context, scope, the worktree path, the check commands, the impeccable skill
folder, the round number and the shoot command for this round.

1. **Read the method.** Impeccable's `SKILL.md` command list, then the
   playbook for each command in your chain, then [`routing.md`](routing.md).
2. **Choose the chain.** Start from the suggestion; change it when the code
   shows a better fit (a "cramped" section whose real cause is a component
   copied three times is `extract`). Fix P0, then P1, then P2; touch P3 only
   in files you are already editing. **`polish` runs last, every round.**
3. **Edit in the worktree only.**
   - Reuse shared components, tokens and utilities before writing new ones.
     Extend a shared component with an optional prop that defaults to
     today's behaviour, never by changing its default.
   - Stay in scope. A file outside it needs a reason, recorded in
     `out_of_scope`.
   - Every colour or surface change must hold in light and dark. Tokens, not
     raw values.
   - Keep the meaning of copy, behaviour and data flow.
   - The repo's `CLAUDE.md` / `AGENTS.md` rules apply.
4. **Prove it or revert.** Run every check command. All green, or fix it.
   If you can't, `git checkout -- . && git clean -fd -- <paths you added>`
   and return `status: reverted` with why. If a component's rendered output
   changed and it has a test, update the test to the new intent; never delete
   the assertion.
5. **Commit once.** `git add <the files you changed>` (never `-A`), commit
   `ui-refine <slug>: <what got better>` with 1–4 short bullets. No push, no
   branch switching. A failing pre-commit hook counts as red.
6. **Shots.** Wait for the dev server to reload, then run the shoot command
   (`--label round-<n>`). Look at them. If a fix doesn't show, or something
   broke (dark mode especially), fix it, rerun checks, amend, reshoot.

Return JSON: `status` (`committed` | `reverted` | `nothing-to-do`),
`commit`, `commands` (in order), `fixed` (ids), `skipped` (id + reason),
`out_of_scope` (file + reason), `checks` (one line), `shots` (paths),
`open_questions`.

---

## Finish reviewer

Inputs: taste file, every problem the fixers claimed `fixed`, the round-0
and final shots, the scope.

You did not watch the fixes, so their claims are not evidence; only the
shots and the code are.

1. For **each claimed fix**, one grade:
   - `fixed`: the final shots (or, for a non-visual fix, the code at its
     location) clearly show what the problem asked for.
   - `partial`: it moved, but the quality asked for isn't there yet.
   - `not-fixed`: can't see it, or it got worse.

   With one line of evidence: the shot and what it shows.
2. **Regressions**: at most 3 things that looked better at round-0 (a theme,
   a width, a section). Nothing else; don't start a new hunt.
3. **Disposition**, one line: "ready for review" only if no P0/P1 is
   `partial` or `not-fixed` and there are no regressions. Otherwise name
   what stays open.

Return JSON: `grades`, `regressions`, `disposition`. Edit nothing.

---

## Ledger

One entry per check, kept short; it is the next checker's only memory.

```
C<n> · <total>/40 (design <d> · audit <a>) · <baseline|better|same|worse> · P0×… P1×… P2×… P3×… · <committed <sha> | reverted | nothing-to-do> [<cmd> → … → polish]
  fixed: <ids> · skipped: <id (why)> · out of scope: <file (why)>
```
