---
name: collect
description: Find real problems and file deduplicated, verified cards in Backlog. Sources are Sentry (errors), PostHog (exceptions, rage and dead clicks, slow pages, failure events, survey themes, tracking gaps), GitHub issues not yet on the board (adopted, not copied), recurring problems in merged PRs' review and QA comments, and a read-only architecture review for refactors. Use for /factory:collect [sentry] [posthog] [github] [prs] [architecture] [--since 30d] [--yes], or when the user asks what's broken in production, what keeps coming up in review, or what to refactor.
argument-hint: "[sentry] [posthog] [github] [prs] [architecture] [--since 14d|2026-09-01] [--yes]"
---

# Collect — turn signals into Backlog cards

You find problems, prove they are real, and file each one **once**. You never
fix anything and never move cards past Backlog: a human grooms them to Ready,
then `/factory:board run` builds them.

Tool: `C=${CLAUDE_PLUGIN_ROOT}/scripts/collect.py` (run with `python3`, from
the repo root). It reads tokens from the environment or the repo's `.env`
by itself. **Never read `.env` yourself**; the guard hooks block it, and
you don't need it. `${CLAUDE_PLUGIN_ROOT}/scripts/env-names.sh` lists which
variable names exist if you need to check.

## Arguments

`/factory:collect github prs architecture --since 30d --yes`

- Sources: any of `sentry`, `posthog`, `github`, `prs`, `architecture`, in
  any order. Anything else is an error: say which words were not
  understood and stop. None given → `python3 $C sources` and use its
  `default` list (Sentry and PostHog when configured; `github`, `prs` and
  `architecture` only when `collect.<source>.enabled` is `true`).
- `--since`: `14d`, `12h`, `2w` or a date like `2026-09-01`. Default
  `collect.since`, else `14d`. `github` ignores it (an old open issue is
  still open work); `prs` reads PRs merged since then; `architecture` uses it
  only to weight recently changed files.
- `--yes`: file without asking. Default is a **dry run**: show what would be
  filed, then ask once. `--yes` never skips dedupe or verification.

## 1. Fetch

For each source: `python3 $C fetch <source> --since <since> > <scratchpad>/<source>.json`.
Read the `status`:

- `ok` → continue. If `partial_errors` is non-empty, report those signals
  (or PRs) as unavailable in the summary. If `raw.truncated` is true, say
  coverage was capped.
- `unavailable` (exit 2) → the source could not be read: say exactly that,
  with the error, and how to fix it (token, scope, host, project id, `gh
  auth`, rate limit). **Never report an unavailable source as "no problems
  found".**
- `disabled` (exit 3) → skip quietly; mention it once in the summary.

`sentry` and `github` return ready `candidates`. `posthog` returns some
candidates plus `raw` material; `prs` and `architecture` return only `raw`
material. Turning raw material into candidates is your job (step 2).

## 2. Build candidates from raw material

Every candidate uses the fetcher's shape: `source`, `kind`, `fingerprint`,
`title`, `summary`, plus `count`/`users`/`first_seen`/`last_seen` when the
source has them, and `evidence`.

### posthog

- **`surveys`**: group free-text responses into themes. A theme is a
  candidate only if 2+ distinct responses describe the same concrete
  problem ("CSV export never finishes"), not general sentiment. Fingerprint:
  `posthog:survey:<kebab-slug-of-theme>`, kind `bug` or `ux`.
- **`pageview_paths`**: compare with the app's routes in the code (router
  config, `pages/` or `app/` directories, route files). A route that exists
  in code but has **no** pageviews in the window is a tracking gap
  candidate, kind `tracking`, fingerprint `posthog:gap:<route>`. Skip
  routes that are obviously internal, admin, API or auth callbacks. Paths
  with dynamic segments match their pattern (`/users/123` ↔ `/users/[id]`).

### github

Nothing to build: each candidate is an open issue that is not on the board,
not a pull request, not already `factory:collected` and not labelled with
one of `collect.github.skip_labels`. It carries `adopt: true` and
fingerprint `github:<number>`. Filing **adopts** it: the original issue is
labelled, gets a short comment and is put in Backlog. It is never copied
into a new issue. Its body stays the author's; anything you add (cause,
suggested acceptance criteria) goes in the adoption comment.

### prs — recurring problems across merged PRs

`raw.prs` lists each merged PR with `notes` (human, bot and factory
comments with a `role`: `qa_fail`, `review_findings`, `review_approved`,
`comment`; inline ones have `path` and `line`) and `bounce_labels`.
`raw.bounce_reasons` maps each `bounce:<reason>` to the PRs that had it.

Cluster by **root cause**, not wording: "date-only strings read in local
time" is one pattern whether it shows up as a QA failure on one PR and a
bot's inline comment on another. A pattern is a candidate **only if it
recurs across 2 or more distinct PRs** (`raw.min_prs`). One-off nits,
style preferences and anything the PR itself settled without a pattern
behind it are not candidates. Look especially for:

1. The same finding shape (same module, same rule) on several PRs, from any
   reviewer.
2. A bounce reason that keeps coming back (`raw.bounce_reasons`) with a
   shared cause in the comments.
3. A Review approval followed by a later PR fixing the very thing it
   approved.

Each candidate: `source: "prs"`, fingerprint `prs:<kebab-slug>` (stable:
name the cause, not the PRs, so next week's run keys the same way), kind
`bug` when there is a live defect in the code, else `process` (the lanes
keep making the same mistake: the fix is a rule, test helper, lint or
skill change that stops it). `last_seen` = the newest PR's `merged_at`.
`evidence.prs` = `[{"number", "role", "quote"}]` for **every** PR in the
cluster, quotes short and taken from the notes; `evidence.files` =
`file:line` locations where it recurs. The script refuses a `prs`
candidate with fewer than 2 PRs, a bad fingerprint or another kind.

### architecture — the refactor finder

Launch **one** fresh, read-only subagent (no edits, no commits, no
branches). Give it the `raw` block (`max_findings`, `context_md`, `adrs`,
`recently_changed`) and this brief:

> Find the few refactors in this repo most worth doing. Read-only: do not
> edit, commit or create files. Use `mattpocock-skills:improve-codebase-architecture`
> for findings only — skip its HTML report and any interactive questions.
> If `ponytail` is installed, also run its over-engineering audit. Read
> `CONTEXT.md` and every ADR under `docs/adr/` first; do not propose
> reversing a recorded decision. Weight recently changed files higher.
> Prefer shallow modules, duplicated concepts, needless indirection and dead
> code over taste. Return at most `max_findings` items as a JSON array:
> `[{"slug": "<kebab-slug of the problem>", "title": "<imperative, under 80 chars>",
>    "what": "<2-3 sentences>", "files": ["path:line", ...],
>    "simplification": "<what gets measurably smaller: lines, files, modules, call sites>",
>    "risk": "<what could break>"}]`
> Every item needs at least one `path:line` you actually read.

Turn each finding into a candidate: `source: "architecture"`, kind
`refactor`, fingerprint `arch:<slug>`, `evidence.files` from the finding,
`summary` from `what`. Its acceptance criteria must stay checkable:
behaviour unchanged, the existing tests still pass unchanged, and the
named simplification measured (e.g. "`src/lib/dates.ts` and
`src/utils/time.ts` become one module; three call sites import it"). The
renderer adds these by default if the verifier gives none. The script
refuses findings without a `path:line`, and keeps only the first
`collect.architecture.max_findings`.

## 3. Dedupe

Put all candidates in one file as `{"candidates": [...]}` and run
`python3 $C dedupe <file>`. Each comes back with a `status`:

- `new` → verify it.
- `duplicate` (open card #N exists) → don't file. If the new numbers are much
  worse (≥2× events or users, or new PRs in a `prs` pattern), add a short
  comment to #N with the new figures.
- `regressed` (card #N was closed, seen again since) → verify; filing will
  reopen #N rather than make a new card.
- `resolved` (closed card #N, not seen since closing) → drop.
- `invalid` → a rule above was broken (`reasons` says which). Fix the
  candidate if you can (more evidence, a proper slug), else drop it and list
  it in the summary.

## 3b. Jev triage (only when `jev.enabled` is true in config)

For each `new` or `regressed` **sentry** or **posthog** candidate, write its
JSON to a file and run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/jev.py decide collect_triage <file> --id <fingerprint>`.
It returns one line of JSON:

- `available: false` → ignore Jev for this candidate; verify as normal.
- `act: true` and `label: "noise"` (live mode, near-certain noise) → don't
  launch a verifier. List it in the summary as *dropped by Jev* with its
  `p_noise`.
- Anything else (shadow mode, or `label: "keep"`) → verify as normal.

Verify candidates in descending `severity` order when Jev gave one. After a
verifier returns, record Claude's answer for every candidate Jev also judged:
`jev.py record collect_triage <fingerprint> noise` if the verdict was
`noise`, otherwise `keep`. This is what `jev.py report` compares against.

## 4. Verify — one fresh verifier per candidate

Launch one subagent per `new` or `regressed` candidate, all in one message
(at most 5 at a time). Give each only its candidate JSON and this brief:

> Decide whether this signal is a real, current problem in this repo.
> Investigate the code (read-only: no edits, no commits). Answer with one
> JSON object and nothing else:
> `{"fingerprint": "...", "verdict": "real|already_fixed|duplicate|noise|unclear",
>   "what": "<2-3 sentences, plain language: what goes wrong for users or the team>",
>   "cause": "<likely cause with file:line, or empty>",
>   "repro": ["<step>", ...],
>   "acceptance": ["<checkable criterion>", ...],
>   "duplicate_of": <issue number or null>,
>   "title": "<imperative, specific card title under 80 chars>"}`
>
> - `real`: the code path exists and nothing on the default branch fixes it.
> - `already_fixed`: a commit since `last_seen` clearly fixes it (cite it).
> - `duplicate`: an open issue describes the same root cause (cite it; check
>   `gh issue list --search`).
> - `noise`: bots, browser extensions, a third-party script, a deliberate
>   rate limit, or a test environment. Say which.
> - `unclear`: you can't tell. That is a valid answer; don't guess.
> Acceptance criteria must be checkable by someone running the code
> (a test that fails today, a measured threshold, a visible behaviour).
> For tracking gaps: criteria are that the route sends a `$pageview` (or the
> named event) and that a test or manual check confirms it.
> For `github` (adopted issues): is it still a problem in the code today,
> and is it concrete enough to build? Vague wishes are `unclear`.
> For `prs` patterns: open every PR and quoted comment. `real` only if the
> pattern genuinely recurs and is not already prevented on the default
> branch (a lint rule, shared helper or test now stops it → `already_fixed`).
> For `architecture` refactors: open every `path:line`. `real` only if the
> simplification holds and does not contradict `CONTEXT.md` or an ADR.
> Criteria must say behaviour is unchanged, existing tests still pass, and
> what measurably shrinks.

Verifiers must not see each other's work. Merge each verdict into its
candidate (`verdict`, `what`, `cause`, `repro`, `acceptance`, and use the
verifier's `title`; for `github` keep the issue's own title).

Filing rules:

| verdict | action |
|---|---|
| `real` | file (adopt, for `github`) |
| `unclear` | file with `needs-triage` (the filer adds the label and a warning) |
| `already_fixed`, `duplicate`, `noise` | don't file; list in the summary with the reason |

## 5. Show, confirm, file

Show a compact table: source, kind, title, evidence (events/users, PR
numbers, or files), verdict, action (`file`, `adopt`, `reopen`, `skip`).
Then, unless `--yes` was given, ask once: file these N cards?

On yes, write the cards to file as a JSON array and run
`python3 $C file <file>`. It creates (or, for regressions, reopens; for
`github`, adopts) each issue with labels `factory:collected`,
`source:<source>`, `kind:<kind>`, adds it to the project and moves it to
**Backlog**. Use `python3 $C render <card.json>` first if you want to
preview one body. It refuses the whole batch if any `prs` or
`architecture` card breaks the rules in step 2. Before filing, render each
new card and check it with
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py card <body> --title "<title>"`;
fix the verifier's title or criteria until it passes.

## 6. Summary

A few lines: filed (#links), adopted, reopened, skipped with reasons, and
per source whether it was read (with counts), capped, unavailable or
disabled. Cards stay in Backlog; remind the user that moving them to Ready
is how they get built.

## Rules

- Read-only towards Sentry, PostHog and the code: never resolve, ignore or
  mute anything there, never edit code, never open PRs. `architecture` and
  every verifier are read-only subagents.
- One card per fingerprint, ever. Dedupe before verifying, and never file
  a candidate that `dedupe` didn't see.
- Adopt, never copy: a GitHub issue that already exists is placed on the
  board as it is.
- Keep personal data out of cards: no emails, user ids, IPs, @-mentions of
  people or full URLs with query strings. Paths, counts, PR numbers and
  short quotes about code only. The script scrubs what it fetches; you keep
  it that way in anything you write.
