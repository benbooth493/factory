# Calibration: learning from a run (optional)

After a run, the lanes' mistakes are on record: QA bounces, review findings,
gate refusals. Calibration hands that record to a `calibrate` skill you have
installed, so it can tighten the skill files that let those mistakes through.
factory doesn't ship a `calibrate` skill and never installs one. It is off
unless you configure it, and it is quiet: when anything is missing it skips
without saying so.

## Config

```json
"calibration": {
  "allow": [
    "skills/build/SKILL.md",
    ".claude/skills/our-conventions/SKILL.md"
  ],
  "skill": "calibrate"
}
```

- `allow`: the **only** files calibration may edit. Paths are relative to
  the repo root (or absolute, or `~/…`), each an existing, canonical
  `SKILL.md` (no symlinks, no wrappers). Empty or missing means calibration is off.
- `skill` (default `calibrate`): the installed skill to delegate to. Check it with
  `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skills-ready.py --skill <skill>`.

The plugin's own files under `${CLAUDE_PLUGIN_ROOT}` change on every plugin
update, so edits there are lost. Point `allow` at skills you own.

## When it runs

At the end of `run`, after the final report, only when **all** of these hold:

1. At least one PR merged in this run, through the merge gate (exit 0 and
   `gh pr view <pr> --json state` says `MERGED`). An empty board, or a run
   that only moved cards to Blocked, teaches nothing reliable.
2. Nothing is still in flight: no card carries `factory:wip`.
3. `calibration.allow` has at least one entry, and every entry exists.
   Drop entries that don't, and skip if none are left.
4. `skills-ready.py --skill <skill>` exits 0.

If any fails, skip. Don't mention it unless the user asks.

## What to hand over

Collect the evidence for **this run only**, short and factual:

- each merged card: issue number, PR, merged SHA
- every bounce: lane, the `bounce:<reason>` label, the first lines of the QA or
  review comment that caused it
- review findings that were fixed before merge (file, what was wrong)
- merge gate refusals and holds, with the gate's message
- anything the user corrected during the run, in their words

Leave out secrets, tokens, `.env` contents, personal data, and anything from
other runs or other repos. Keep it under a few pages; quote excerpts, don't paste logs.

## Delegate

Launch one subagent:

> Use the `<skill>` skill. Here is the evidence from one factory run on
> <repo>: <evidence>. You may edit only these files: <allow list, absolute
> paths>. Do not create files, and do not edit anything else, including
> CLAUDE.md, AGENTS.md, memory, settings or other skills. Anything that
> belongs elsewhere: write it in your reply as a proposal. Reply with what you
> changed in each file and why, citing the evidence.

## Check what it did

1. `git status --porcelain` and `git diff --name-only`, plus the same for any
   allow-listed file outside the repo. Compare against the allow list.
2. Any change outside the list: revert it (`git checkout -- <path>` for tracked
   files, delete new untracked files the subagent made), and tell the user
   what it tried to touch.
3. Never commit or push calibration edits. Tell the user, in two or three
   lines, which skill files changed and why, and that the edits are
   uncommitted for them to review. List proposals separately.

## Rules

- Calibration never changes product code, cards, PRs, config or the board.
- It never runs mid-run or between waves; only after the run's final report.
- One delegation per run. If it fails, report that in one line and stop.
