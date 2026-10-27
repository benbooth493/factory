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
```

Your board's Status field needs these columns: **Backlog, Ready, Build, QA,
Review, Blocked, Done** (names can be remapped in config). Cards need
checkable acceptance criteria; ones without are sent back to Backlog with a
comment instead of being built on a guess.

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

Set `auto_merge: false` to have the gate stop at "all green" and leave the
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
go there if you want the full kit (problem collection from Sentry/PostHog,
UI refinement loops, visual reports, Codex support, model routing ladders).
factory keeps only the core loop: four skills, three scripts, two hooks,
lanes run as in-session subagents, no headless dispatcher. No code was copied.

## Layout

```
.claude-plugin/   plugin.json, marketplace.json
skills/board/     orchestrator: onboard, doctor, status, run, stop
skills/build/     Build lane
skills/qa/        QA lane
skills/review/    Review lane
scripts/          board.sh (gh project wrapper), worktree.sh, merge-gate.sh
hooks/            guard_bash.py, guard_files.py, hooks.json
tests/            hook unit tests; board and gate tests against a fake gh
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
- Draft issues and PR cards on the board are ignored; only issues from the
  configured repo are worked.

MIT © Ben Booth
