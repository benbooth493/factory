# Card router

Before a wave dispatches a card, the orchestrator grades it **easy**,
**medium** or **hard**. The grade and the run flags pick the lane model.
`scripts/route.py` is the only place a grade becomes a model; nothing else
keeps a grade → model table.

`R="python3 ${CLAUDE_PLUGIN_ROOT}/scripts/route.py"`

## 1. Read the run flags

`$R args <the run arguments>` prints `{"host", "tier", "pin"}`:

- `--low` → tier `low`; `--high` → tier `high`; neither → `default`.
- `--codex` → host `codex`; `--codex=<model>` also sets `pin`.
- Inside a Codex session (`CODEX_THREAD_ID` set) the host is `codex` with
  no flag. `FACTORY_HOST=claude|codex` forces it.

Keep these for the whole run, and repeat them in the resume hint.

## 2. Grade each card, first answer wins

Skip grading when `$R router --tier T --host H [--pin P]` prints `none`
(a pinned model works every card).

1. **The card says so.** `gh issue view <n> --json body,labels > card.json`,
   then `$R card card.json` prints `{"grade", "source"}`. A
   `difficulty:easy|medium|hard` label or a `Difficulty: <grade>` line in
   the body is a person's call and always wins.
2. **Jev, when live.** If `jev.points.card_lint` is `live`, pass the lint
   decision too: `$R card card.json --jev lint.json`. Its difficulty counts
   only when `available` and `act` are both true.
3. **Ask the router.** If `grade` is still null, run one subagent on the
   model `$R router ...` prints, with the prompt below. On Codex run the same
   prompt with `codex exec -m <router model> -` (read-only), and take the last
   line. Anything other than one of the three words counts as `medium`.

A card that enters the wave past Ready (QA, Review) is not re-graded; use
the grade recorded in its Build hand-off comment, else `medium`.

## 3. Pick the lane model

`$R model --lane <lane> --grade <grade> --tier T --host H [--pin P]` prints
the model. `inherit` means: launch the subagent without a model, so it runs
on the session's own model. On Codex, pass the result to
`codex-lane.sh <lane> <n> --model <model>`.

Precedence: `--codex=<model>` > `codex.model` (Codex) > `models.<lane>`
(Claude) > `routing.<host>.<tier>.<grade>` > the built-in ladder.

| Run | Router | Easy | Medium | Hard |
|---|---|---|---|---|
| `run` | haiku | sonnet | opus | session model |
| `run --low` | haiku | haiku | sonnet | opus |
| `run --high` | sonnet | opus | opus | opus |
| `run --codex` | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol | gpt-6-astra |
| `run --codex --low` | gpt-6-luna | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol |
| `run --codex --high` | gpt-6-luna | gpt-6-astra | gpt-6-astra | gpt-6-astra |
| `run --codex=<model>` | none | model | model | model |

Mention the grade in the wave line: `wave 2: build #18 (hard → session)`.

## Router prompt

Use exactly this, with `<n>`, `<repo>` filled in. It reads; it changes
nothing.

> You grade one GitHub issue for how much engineering work it needs. You do
> not write code, comment, label or move anything.
>
> Read issue #<n> in <repo>: `gh issue view <n> --repo <repo> --json title,body,comments`.
> You may look at the repository to judge how much code is involved.
>
> - **easy**: a small, local change in one or two files with an obvious
>   approach and an obvious test.
> - **medium**: several files, or one area the change has to learn first, or
>   a test that needs some setup.
> - **hard**: cross-cutting design, a data migration, concurrency, security
>   or auth, a public API change, or an approach the issue leaves open.
>
> When torn between two grades, pick the higher one. Reply with one word on
> the last line: `easy`, `medium` or `hard`.
