# factory

**Put cards in Ready. Get merged PRs with evidence.**

A small Claude Code plugin that works a GitHub Project board for you. Each
card goes through three lanes, each run by its own subagent:

```
Ready ──► Build ──► QA ──► Review ──► Done
            ▲        │       │
            └────────┴───────┘  bounced (max 2, then Blocked)
```

- **Build** makes a branch in its own worktree, writes the test first, makes
  the smallest change that meets the acceptance criteria, opens a draft PR.
- **QA** proves each acceptance criterion against the exact commit, with
  evidence, or bounces it back with what failed.
- **Review** reads the diff cold, approves a specific SHA, and merges only
  through the merge gate.

The board is the only state. Stop at any point and `run` again; it picks up
where the cards are.

## Install

```
/plugin marketplace add benbooth493/factory
/plugin install factory@factory
```

Needs `gh` (signed in with the `project` scope: `gh auth refresh -s project`),
`jq`, `git`, bash 3.2+ and Python 3.9+.

## Use

```
/factory:board onboard     # pick a project, check columns, set verify commands
/factory:board doctor      # check auth, columns and config
/factory:board run         # drain the board, wave by wave
/factory:board run --once  # one wave, then stop
/factory:board status      # read-only snapshot
/factory:board stop        # release claims and leave a note on each card
/factory:collect           # find problems in Sentry + PostHog, file Backlog cards
```

Your board's Status field needs these columns: **Backlog, Ready, Build, QA,
Review, Blocked, Done** (names can be remapped in config). Cards need
checkable acceptance criteria; ones without are sent back to Backlog with a
comment instead of being built on a guess.

## Collect: Sentry and PostHog → Backlog

```
/factory:collect                      # every enabled source, last 14 days, dry run first
/factory:collect sentry --since 30d   # one source, custom window
/factory:collect posthog --since 2026-09-01 --yes
```

| Source | What it looks for |
|---|---|
| Sentry | unresolved issues seen in the window, above `min_events` / `min_users`, with the top in-app stack frames |
| PostHog | `$exception` groups, rage clicks, dead clicks, slow pages (LCP p75 over budget), your own failure events, recurring survey complaints, and routes in the code that never send a pageview (tracking gaps) |

Each candidate is deduped against cards already filed (by a fingerprint in
the issue body), then checked by its own fresh verifier subagent that reads
the code and returns *real*, *already fixed*, *duplicate*, *noise* or
*unclear*. Real ones are filed; unclear ones are filed with `needs-triage`;
the rest are listed with the reason. A problem whose card was closed but has
been seen again is **reopened** as a regression rather than filed twice.

Cards land in **Backlog** with evidence, likely cause, repro steps and
acceptance criteria, labelled `factory:collected`, `source:*`, `kind:*`.
Moving one to Ready is how it gets built.

Tokens come from the environment or your repo's `.env` (read by the script,
never by the agent):

| Variable | Scopes |
|---|---|
| `SENTRY_AUTH_TOKEN` | `event:read`, `project:read` |
| `POSTHOG_PERSONAL_API_KEY` | `query:read` |

A source that can't be reached is reported as **unavailable** with the
reason, never as "nothing found". Collect is read-only towards Sentry and
PostHog, and strips query strings and user identifiers from what it files.

## Config

`.claude/factory.json` in your repo (`onboard` writes it):

```json
{
  "owner": "benbooth493",
  "project_number": 1,
  "repo": "benbooth493/my-app",
  "base_branch": "main",
  "verify_commands": ["make test"],
  "max_parallel": 3,
  "max_bounces": 2,
  "merge_method": "squash",
  "auto_merge": true,
  "models": { "build": "sonnet", "qa": "sonnet", "review": "opus" }
}
```

Add a `collect` block for Sentry and PostHog (`onboard` detects both and
tests the connection). Set `auto_merge: false` to have the gate stop at "all green" and leave the
merge button to you. Full example in [`examples/factory.json`](examples/factory.json).

## Safety

**Merge gate** ([`scripts/merge-gate.sh`](scripts/merge-gate.sh)) is the only
way factory merges. It refuses unless the PR head is exactly the commit the
reviewer approved, every check has passed, there are no conflicts, and your
`verify_commands` pass on the head merged with the current base. The merge
is pinned with `--match-head-commit`, so a push that sneaks in afterwards
makes GitHub reject it. No `verify_commands`, no merge.

**Guard hooks** run on every tool call while the plugin is enabled:

| Blocks | Why |
|---|---|
| `git worktree add` outside `.claude/worktrees/` | keeps parallel lanes out of each other's way |
| `rm`, `find -delete`, `git clean` aimed at the project root or anything outside it | an agent's typo shouldn't cost you your home directory |
| `git push --force` / `+refspec`, pushes to `main`, `master` or your base branch, remote branch deletion | only the gate touches main; `--force-with-lease` is fine on feature branches |
| reading `.env*`, SSH keys, cloud credentials, `*.pem` (shell or Read tool) | secrets stay out of the transcript |
| writing a live-looking API key into a file | read it from the environment instead |

**Claims**: the `factory:wip` label is a per-card mutex, so two lanes never
work the same card. `stop` releases them; `run` clears stale ones left by a
crashed session.

## How it differs from super-board

This is a deliberately small reimplementation of the idea in
[EricTechPro/super-board](https://github.com/EricTechPro/super-board) —
go there if you want the full kit (GitHub-issue and PR-pattern collection,
architecture scans, UI refinement loops, visual reports, Codex support, model
routing ladders). factory keeps the core loop plus Sentry/PostHog collection:
five skills, a handful of scripts, two hooks, lanes run as in-session
subagents, no headless dispatcher. No code was copied.

## Layout

```
.claude-plugin/   plugin.json, marketplace.json
skills/board/     orchestrator: onboard, doctor, status, run, stop
skills/build/     Build lane
skills/qa/        QA lane
skills/review/    Review lane
skills/collect/   Sentry + PostHog → verified Backlog cards
scripts/          board.sh (gh project wrapper), worktree.sh, merge-gate.sh,
                  collect.py (fetch, dedupe, render, file), env-names.sh
hooks/            guard_bash.py, guard_files.py, hooks.json
tests/            hook and collector unit tests (recorded Sentry/PostHog payloads);
                  board and gate tests against a fake gh
```

## Develop

```
python3 -m unittest discover -s tests
bash tests/test_board.sh
bash tests/test_merge_gate.sh
shellcheck scripts/*.sh
```

## Limits

- One orchestrator per board at a time. Two concurrent `run`s will treat
  each other's claims as stale.
- QA is only as good as what can be run headlessly in your repo. UI
  evidence needs Playwright in the project.
- The Sentry and PostHog queries are tested against recorded payloads, not
  yet against live accounts. PostHog exception grouping depends on SDK
  version; if your exceptions come out ungrouped, tell us.
- Draft issues and PR cards on the board are ignored; only issues from the
  configured repo are worked.

MIT © Ben Booth
