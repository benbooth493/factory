# factory

**Put cards in Ready. Get merged PRs with evidence.**

A Claude Code plugin that works a GitHub Project board for you. Each card
goes through three lanes, each run by its own subagent (or by Codex):

```
Ready ──► Build ──► QA ──► Review ──► Done
            ▲        │       │
            └────────┴───────┘  bounced (max 2, then Blocked)
```

- **Build** makes a branch in its own worktree, writes the test first, makes
  the smallest change that meets the acceptance criteria, opens a draft PR.
- **QA** proves each acceptance criterion against the exact commit, with
  evidence (screenshots at three screen sizes for UI cards), or bounces it
  back with what failed and why.
- **Review** reads the diff cold, approves a specific SHA, and merges only
  through the merge gate, or holds it for a human when policy says so.

The board is the only state. Stop at any point and `run` again; it picks up
where the cards are. Docs and an interactive skill map:
https://benbooth493.github.io/factory/

## Install

```
/plugin marketplace add benbooth493/factory
/plugin install factory@factory
```

Needs `gh` (signed in with the `project` scope: `gh auth refresh -s project`),
`jq`, `git`, bash 3.2+ and Python 3.9+.

factory also **requires three skill packs**, which the lanes load as they
work: Matt Pocock's skills, impeccable and humanizer.

```
/plugin install mattpocock-skills@claude-plugins-official
/plugin marketplace add pbakaus/impeccable
/plugin install impeccable@impeccable
/plugin marketplace add blader/humanizer
/plugin install humanizer@humanizer
```

`python3 scripts/skills-ready.py --all` checks them and prints the install
commands for anything missing; `onboard`, `doctor` and every `run` run it too.
`ponytail` is optional (an inline "simplest solution first" check stands in).

## Use

```
/factory:board onboard               # project, columns, skills, sources, merge policy
/factory:board doctor                # check auth, columns, config and skills
/factory:board lint                  # find vague cards and tighten them with you
/factory:board migrate --dry-run     # add missing columns, map legacy ones
/factory:board run                   # drain the board, wave by wave
/factory:board run --once            # one wave, then stop
/factory:board run --low             # cheaper models per card
/factory:board run --high            # Opus on every card
/factory:board run --codex           # every lane on OpenAI Codex (--codex=<model> pins one)
/factory:board status                # read-only snapshot
/factory:board stop                  # release claims and leave a note on each card
/factory:collect                     # find problems, file verified Backlog cards
/factory:ui-refine-loop <route>      # standalone impeccable polish loop → draft PR
/factory:visual [recap|plan|<path>]  # one HTML page: branch recap, plan, or codebase map
/factory:git-sync                    # commit by logical change, merge-pull, push (never force)
```

Your board's Status field needs these columns: **Backlog, Ready, Build, QA,
Review, Blocked, Done** (names can be remapped in config; `migrate` adds
missing ones and moves cards out of legacy columns like Building or Todo).
Cards need checkable acceptance criteria; ones without are sent back to
Backlog with the flagged lines instead of being built on a guess.

## Skills

<!-- skills:start -->
**You type**

| Skill | What it does |
|---|---|
| [`board`](skills/board/SKILL.md) | Orchestrator: onboard, doctor, status, lint, migrate, run and stop; runs the three lanes wave by wave. |
| [`collect`](skills/collect/SKILL.md) | Finds problems in Sentry, PostHog, GitHub issues, merged PRs and the architecture, and files verified, deduplicated Backlog cards. |
| [`ui-refine-loop`](skills/ui-refine-loop/SKILL.md) | Critique then refine one page or component until it looks right, with screenshots. |
| [`visual`](skills/visual/SKILL.md) | One self-contained HTML page: a branch recap, a plan, or a map of a folder. |
| [`git-sync`](skills/git-sync/SKILL.md) | Commit per logical change, merge-pull, push without force, report. |

**The board runs**

| Skill | What it does |
|---|---|
| [`build`](skills/build/SKILL.md) | Build lane: worktree, test first, smallest change, draft PR. |
| [`qa`](skills/qa/SKILL.md) | QA lane: evidence for each acceptance criterion, or bounce to Build. |
| [`review`](skills/review/SKILL.md) | Review lane: reads the diff cold, approves one SHA, merges through the gate. |
<!-- skills:end -->

## Models

A cheap router grades each card easy, medium or hard; the run flag picks the
model for each grade.

| Run | Router | Easy | Medium | Hard |
|---|---|---|---|---|
| `run` | haiku | sonnet | opus | session model |
| `run --low` | haiku | haiku | sonnet | opus |
| `run --high` | sonnet | opus | opus | opus |
| `run --codex` | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol | gpt-6-astra |
| `run --codex --low` | gpt-6-luna | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol |
| `run --codex --high` | gpt-6-luna | gpt-6-astra | gpt-6-astra | gpt-6-astra |
| `run --codex=<model>` | none | model | model | model |

A card's own `Difficulty: easy|medium|hard` line or `difficulty:*` label wins
over the router, and Jev can grade cards once its `card_lint` point is live.
`models.<lane>` still overrides the ladder on Claude, and `routing` in config
changes any cell. Codex details and what's tested: [docs/hosts.md](docs/hosts.md).

## Collect: problems → Backlog

```
/factory:collect                          # every enabled source, last 14 days, dry run first
/factory:collect sentry --since 30d       # one source, custom window
/factory:collect github prs architecture  # the opt-in sources
/factory:collect posthog --since 2026-09-01 --yes
```

| Source | What it looks for |
|---|---|
| Sentry | unresolved issues seen in the window, above `min_events` / `min_users`, with the top in-app stack frames |
| PostHog | `$exception` groups, rage clicks, dead clicks, slow pages (LCP p75 over budget), your own failure events, recurring survey complaints, and routes in the code that never send a pageview (tracking gaps) |
| GitHub | open issues not on the board yet; **adopted** into Backlog (labelled, commented), never copied |
| PRs | problems that recur across 2+ recently merged PRs: human, bot and factory Review comments, `🧪 QA FAIL` reports, `bounce:*` labels |
| Architecture | a read-only refactor finder (`mattpocock-skills:improve-codebase-architecture`, plus ponytail's audit if installed) that respects `CONTEXT.md` and `docs/adr/`; at most `max_findings` refactor cards with file:line evidence |

GitHub, PRs and Architecture are opt-in: set `collect.<source>.enabled` to
true, or name them.

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

## UI: screenshots and the refine loop

For any card that touches UI, QA starts the app (`ui.start_command`), takes
full-page shots at 1920×1080, 1024×768 and 375×667, commits them to the PR
branch and embeds them in its comment, on pass and on fail.

`/factory:ui-refine-loop <route|component>` is separate from the board: it
asks you a few questions once to write a taste file, then runs bounded
rounds where a fresh checker runs impeccable's critique and audit and a fresh
fixer applies the matching impeccable commands, keeping checks green or
reverting. It ends in a draft PR with a Before | After table (light and dark,
1440 and 390 wide).

## Writing standard

Commits, PR titles and bodies, cards and lane comments all follow
[`docs/writing-standard.md`](docs/writing-standard.md): imperative subjects
of 72 characters or fewer, bodies that say why, plain words, required PR and
card sections. `python3 scripts/check-writing.py commit|pr|card|comment <file>`
checks the mechanical rules; the lanes run it before they push or post.
PR and card prose gets a `humanizer` pass first.

## Jev: fast typed decisions (optional)

[Jev](https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe) from
TypeSafe doesn't write text; it answers questions you define, with a
probability for each answer, in well under a second and for a fraction of a
cent. factory calls it through Vercel AI Gateway at four points where a quick
judgement is enough:

| Point | Question | When live, it… |
|---|---|---|
| `collect_triage` | Is this Sentry/PostHog signal actionable, noise or expected? How severe? | drops near-certain noise (P ≥ 0.9) before a Claude verifier is spent on it |
| `card_lint` | Does the card have checkable criteria? How hard is it? | grades the card for the router (`scripts/route.py` picks the model) |
| `risk_veto` | Does the PR touch auth, payments, secrets, migrations, user-data deletion or cloud permissions? | holds the merge for a human (`factory:needs-human`). **It can only block, never approve** |
| `bounce_reason` | Missing test, logic bug, misread criteria, scope creep or environment? | labels the bounce so Build starts in the right place |

Every point starts in **shadow**: Jev's answer is logged next to what Claude
decided at the same moment, and nothing acts on it.

```
python3 scripts/jev.py check     # connectivity, model, cost of one call
python3 scripts/jev.py report    # agreement and spend per decision point
```

`report` says "ready for live" once a point has 30+ comparisons, at least
90% agreement and none of the expensive mistakes (dropping a bug Claude kept,
waving through a change Claude flagged). Then set that point to `"live"` in
config. If Jev is unreachable, out of credit or over `monthly_budget_usd`,
factory carries on exactly as it would without it.

Key: `AI_GATEWAY_API_KEY` in the environment or `.env`. Decisions are logged
to `.claude/factory/jev-log.jsonl` (keep it out of git).

## Config

`.claude/factory.json` in your repo (`onboard` writes it). The minimum:

```json
{
  "owner": "benbooth493",
  "project_number": 1,
  "repo": "benbooth493/my-app",
  "base_branch": "main",
  "verify_commands": ["make test"]
}
```

Everything else is optional and documented by example in
[`examples/factory.json`](examples/factory.json): `merge_policy` and
`migrations`, `routing` and `codex`, `ui`, `collect`, `jev`, `lint`,
`calibration`, `protected_branches`, column names, parallelism and bounce
limits. Set `auto_merge: false` to have the gate stop at "all green" and
leave the merge button to you.

## Safety

**Merge gate** ([`scripts/merge-gate.sh`](scripts/merge-gate.sh)) is the only
way factory merges. It refuses unless the PR head is exactly the commit the
reviewer approved, every check has passed, there are no conflicts, and your
`verify_commands` pass on the head merged with the current base. The merge
is pinned with `--match-head-commit`, so a push that sneaks in afterwards
makes GitHub reject it. No `verify_commands`, no merge.

A `merge_policy` decides which cards a robot may merge (by `kind:*` label,
required or blocking labels, size); anything else is **held for a human**:
the PR and card get `factory:needs-human`, the reasons are commented on the
PR, and later waves leave the card alone. A PR that adds migrations is held
unless `migrations.allowed` is true; then `migrations.command` runs only
after the merge succeeds, and a failure is posted loudly on the PR. With
Jev's `risk_veto` live, a green but sensitive change is held the same way.

**Guard hooks** run on every tool call while the plugin is enabled:

| Blocks | Why |
|---|---|
| `git worktree add` outside `.claude/worktrees/` | keeps parallel lanes out of each other's way |
| `rm`, `find -delete`, `git clean` aimed at the project root or anything outside it | an agent's typo shouldn't cost you your home directory |
| `git push --force` / `+refspec`, pushes to `main`, `master`, your base branch or `protected_branches`, remote branch deletion | only the gate touches main; `--force-with-lease` is fine on feature branches |
| reading `.env*`, SSH keys, cloud credentials, `*.pem` (shell or Read tool) | secrets stay out of the transcript (`scripts/env-names.sh` lists variable names only) |
| writing a live-looking API key into a file | read it from the environment instead |

Two more hooks tidy up: merged PRs' worktrees are removed at session start
and after each merge (branch tips logged to `.claude/factory/cleanup.log`
first), and the skills table above regenerates when a skill changes.

**Claims**: the `factory:wip` label is a per-card mutex, so two lanes never
work the same card. `stop` releases them; `run` clears stale ones left by a
crashed session.

## How it relates to super-board

factory is an independent reimplementation of the idea in
[EricTechPro/super-board](https://github.com/EricTechPro/super-board), and
now covers its feature set: the Build/QA/Review loop, collection from
Sentry, PostHog, GitHub issues, PR patterns and an architecture scan, the UI
refine loop, visual pages, git-sync, the writing standard, the model router
with Codex support, merge policies and migrations, card lint, board
migration, calibration, cleanup hooks and the docs site. The differences
that remain:

- **factory only:** Jev decisions in shadow mode, a Blocked column with
  bounce counts and bounce reasons, PR holds that later waves skip,
  push protection always on, regressions reopen their old card, personal
  data stripped from collected cards.
- **super-board only:** the legacy headless `claude -p` dispatcher and
  live-model evals.
- **Different:** lanes run as in-session subagents (or `codex exec`), not a
  workflow runtime; claims use a label, not the assignee.

No code was copied; super-board's MIT-licensed source was read for
behaviour.

## Layout

```
.claude-plugin/        plugin.json, marketplace.json
skills/board/          orchestrator: onboard, doctor, status, lint, migrate, run, stop
skills/build|qa|review the three lanes the board runs
skills/collect/        problems → verified Backlog cards
skills/ui-refine-loop/ standalone UI refine loop (impeccable, humanizer)
skills/visual/         one-page HTML recap, plan or codebase map
skills/git-sync/       commit, merge-pull, push
scripts/               board.sh, worktree.sh, merge-gate.sh, board-migrate.sh,
                       lint-cards.py, route.py, codex-lane.sh, collect.py, jev.py,
                       skills-ready.py, check-writing.py, git-sync.sh, shoot.mjs,
                       serve-check.sh, visual.py, skill-map.py, build-site.py,
                       readme-sync.py, env-names.sh
hooks/                 guards, worktree cleanup, hooks.json
docs/                  writing-standard.md, hosts.md
site/                  docs site template (built by scripts/build-site.py → _site/)
tests/                 offline tests: recorded API payloads, fake gh/codex, temp git repos
```

## Develop

```
python3 -m unittest discover -s tests
bash tests/test_board.sh
bash tests/test_merge_gate.sh
bash tests/test_board_migrate.sh
bash tests/test_git_sync.sh
bash tests/test_codex_lane.sh
bash tests/test_shoot_refine.sh
python3 scripts/readme-sync.py --check
python3 scripts/build-site.py --out _site
shellcheck scripts/*.sh tests/*.sh
```

## Limits

- Not yet run end to end against a live board, Sentry, PostHog, Jev or Codex;
  every external API is tested against recorded payloads or fakes.
- One orchestrator per board at a time. Two concurrent `run`s will treat
  each other's claims as stale.
- QA is only as good as what can be run headlessly in your repo. UI evidence
  needs Playwright in the project (`npm i -D playwright && npx playwright
  install chromium`) and a way to start the app (`ui.start_command`); pages
  behind a sign-in show the sign-in screen.
- Jev is early access, and its accuracy and calibration figures are mostly
  the vendor's own. That's why every point starts in shadow.
- Under Codex the guard hooks don't run; only Codex's sandbox and the lane
  prompt enforce them.
- Draft issues and PR cards on the board are ignored; only issues from the
  configured repo are worked.

MIT © Ben Booth
