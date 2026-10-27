---
name: collect
description: Find real problems in Sentry and PostHog (errors, exceptions, rage and dead clicks, slow pages, failure events, survey themes, tracking gaps), verify each one, and file deduplicated cards in Backlog. Use for /factory:collect [sentry] [posthog] [--since 30d] [--yes], or when the user asks what's broken in production.
argument-hint: "[sentry] [posthog] [--since 14d|2026-09-01] [--yes]"
---

# Collect — turn production signals into Backlog cards

You find problems, prove they are real, and file each one **once**. You never
fix anything and never move cards past Backlog: a human grooms them to Ready,
then `/factory:board run` builds them.

Tool: `C=${CLAUDE_PLUGIN_ROOT}/scripts/collect.py` (run with `python3`, from
the repo root). It reads tokens from the environment or the repo's `.env`
by itself. **Never read `.env` yourself**; the guard hooks block it, and
you don't need it. `${CLAUDE_PLUGIN_ROOT}/scripts/env-names.sh` lists which
variable names exist if you need to check.

## Arguments

- Sources: any of `sentry`, `posthog`. None given → every source enabled
  under `collect` in `.claude/factory.json`.
- `--since`: `14d`, `12h`, `2w` or a date like `2026-09-01`. Default
  `collect.since`, else `14d`.
- `--yes`: file without asking. Default is a **dry run**: show what would be
  filed, then ask once.

## 1. Fetch

For each source: `python3 $C fetch <source> --since <since> > /tmp/…/<source>.json`
(use your scratchpad). Read the `status`:

- `ok` → continue. If `partial_errors` is non-empty, report those signals as
  unavailable in the summary.
- `unavailable` (exit 2) → the source could not be read: say exactly that,
  with the error, and how to fix it (token, scope, host, project id). **Never
  report an unavailable source as "no problems found".**
- `disabled` (exit 3) → skip quietly; mention it once in the summary.

## 2. Turn raw PostHog material into candidates

The fetch output's `raw` block needs judgement, so it is yours:

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

Use the same candidate shape as the fetcher (`source`, `kind`,
`fingerprint`, `title`, `summary`, `count`, `users`, `evidence`).

## 3. Dedupe

Put all candidates in one file as `{"candidates": [...]}` and run
`python3 $C dedupe <file>`. Each comes back with a `status`:

- `new` → verify it.
- `duplicate` (open card #N exists) → don't file. If the new numbers are much
  worse (≥2× events or users), add a short comment to #N with the new figures.
- `regressed` (card #N was closed, seen again since) → verify; filing will
  reopen #N rather than make a new card.
- `resolved` (closed card #N, not seen since closing) → drop.

## 3b. Jev triage (only when `jev.enabled` is true in config)

For each `new` or `regressed` candidate, write its JSON to a file and run
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

> Decide whether this production signal is a real, current problem in this
> repo. Investigate the code (read-only: no edits, no commits). Answer with
> one JSON object and nothing else:
> `{"fingerprint": "...", "verdict": "real|already_fixed|duplicate|noise|unclear",
>   "what": "<2-3 sentences, plain language: what goes wrong for users>",
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

Verifiers must not see each other's work. Merge each verdict into its
candidate (`verdict`, `what`, `cause`, `repro`, `acceptance`, and use the
verifier's `title`).

Filing rules:

| verdict | action |
|---|---|
| `real` | file |
| `unclear` | file with `needs-triage` (the renderer adds the label and a warning) |
| `already_fixed`, `duplicate`, `noise` | don't file; list in the summary with the reason |

## 5. Show, confirm, file

Show a compact table: source, kind, title, events/users, verdict, action.
Then, unless `--yes` was given, ask once: file these N cards?

On yes, write the cards to file as a JSON array and run
`python3 $C file <file>`. It creates (or, for regressions, reopens) each
issue with labels `factory:collected`, `source:<source>`, `kind:<kind>`,
adds it to the project and moves it to **Backlog**. Use
`python3 $C render <card.json>` first if you want to preview one body.

## 6. Summary

A few lines: filed (#links), reopened, skipped with reasons, and any source
that was unavailable or disabled. Cards stay in Backlog; remind the user that
moving them to Ready is how they get built.

## Rules

- Read-only towards Sentry and PostHog: never resolve, ignore or mute
  anything there.
- One card per fingerprint, ever. Dedupe before verifying, and never file
  a candidate that `dedupe` didn't see.
- Keep personal data out of cards: no emails, user ids, IPs or full URLs
  with query strings. Paths and counts only.
