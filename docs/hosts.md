# Hosts: Claude Code and Codex

factory is written for Claude Code. It can also hand every lane to OpenAI
Codex, from a Claude session or from inside Codex itself.

| | Start a run | Lanes run as | Status |
|---|---|---|---|
| Claude Code | `/factory:board run [--low\|--high]` | in-session subagents | the reference host; tested |
| Claude Code → Codex | `/factory:board run --codex[=<model>] [--low\|--high]` | `scripts/codex-lane.sh` → `codex exec` | launcher tested against a fake `codex` only |
| Inside Codex | `$board run` | `codex exec` per lane, or inline | **untested** |

Every host also needs `gh` (with the `project` scope), `jq`, `git`, bash
3.2+ and Python 3.9+. Codex needs `npm i -g @openai/codex` and `codex login`.

## Which host am I in?

`python3 scripts/route.py host` prints `claude` or `codex`:

1. `FACTORY_HOST=claude|codex` wins (tests, or a host it misreads).
2. `CODEX_THREAD_ID` or `CODEX_SESSION_ID` set → `codex`. Codex sets these in
   every shell it runs (super-board recorded them on codex-cli 0.160.0).
3. Otherwise `claude`.

`run --codex` sets the host to `codex` regardless.

## Models

The router grades each card; the flag and host pick the model
(`skills/board/references/router.md` has the full table and the prompt).
`--codex=<model>` or config `codex.model` pins one model for every lane and
skips the router. Per-lane `models.<lane>` apply to Claude lanes only.

Override any cell in config:

```json
"routing": {
  "claude": { "default": { "hard": "opus" } },
  "codex":  { "low": { "router": "gpt-6-luna", "easy": "gpt-6-luna" } }
}
```

The Codex model names (`gpt-6-luna`, `gpt-6.1-sol`, `gpt-6-astra`) are
taken from super-board's ladder. factory has not called them.

## Codex lanes from Claude (`--codex`)

The orchestrator plans, claims and reconciles exactly as usual. Instead of a
subagent it runs, for each card in the wave, in parallel:

```
bash ${CLAUDE_PLUGIN_ROOT}/scripts/codex-lane.sh <lane> <n> --model <model>
```

The launcher:

- runs `codex exec -C <repo root> -c approval_policy="never" -m <model> --output-last-message <file> -`
  with the prompt on stdin. The prompt tells Codex to read
  `skills/<lane>/SKILL.md` and follow it, to do every step itself (no
  sub-agents), and to keep the guard-hook rules by hand;
- sandboxes it with a permission profile: read everywhere, write only to the
  repo, its git dir and temp dirs, network on (lanes need `gh` and `git push`);
- exports `CLAUDE_PLUGIN_ROOT` and `FACTORY_CONFIG` so the skill's paths work;
- kills Codex after `codex.timeout_minutes` (default 60): exit 124;
- prints the lane's `RESULT: <lane> #<n> ...` line and exits 0, or exits 3
  when there is none. Codex's transcript is in
  `.claude/factory/codex/<lane>-<n>.log`.

The orchestrator treats exit 3 or 124 like a lane that died: release the
claim, comment, leave the card for the next wave.

**Not the same as Claude lanes:** factory's guard hooks are Claude Code
hooks and do not run inside Codex. The sandbox keeps writes inside the repo,
and the merge gate is a script, so it still refuses unreviewed heads and
red checks; but force-push and `.env` reads are only refused by the prompt.

### Tested vs untested

| Piece | Status |
|---|---|
| Prompt content, model flag, timeout, RESULT extraction, exit codes | tested with `tests/fakebin_route/codex` (`tests/test_codex_lane.sh`) |
| `exec`, `-C`, `-m`, `-c approval_policy`, `--output-last-message`, `-` for stdin | match super-board's invocation recorded on codex-cli 0.160.0; not run by factory |
| `default_permissions` / `permissions.<name>.filesystem` / `network.enabled` profile | same source; not run by factory |
| A full lane (Build → PR) on real Codex | **untested** |

## Running factory inside Codex

**Untested.** What should work, based on how Codex finds skills:

Codex has no plugins and no `/factory:board` command. It loads skills from
`.agents/skills/` in the repo (and `~/.agents/skills/`), following symlinks,
and you call one with `$<name>`. Link factory's skills there and point
`CLAUDE_PLUGIN_ROOT` at the checkout:

```
git clone https://github.com/benbooth493/factory ~/src/factory
mkdir -p .agents/skills
for s in board build qa review collect; do ln -s ~/src/factory/skills/$s .agents/skills/$s; done
export CLAUDE_PLUGIN_ROOT=~/src/factory
codex
> $board run
```

Inside Codex, `route.py` reports host `codex`, so plain `$board run` uses the
Codex ladder (`--low`, `--high` and `--codex=<model>` still apply). The board
skill can run each lane with `codex-lane.sh` as above, which gives every
lane a fresh context; a Codex session that cannot start a nested `codex exec`
should run the lanes inline, one card at a time, reading the lane SKILL.md
itself. Either way the guard hooks are not active.

Known gaps: no hooks, no parallel subagents in-session, the `$board` names
can clash with other skills called `build` or `review` in `.agents/skills/`
(rename the link, e.g. `factory-build`, and nothing else changes, since the
board skill reads lane files by path).
