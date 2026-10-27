---
name: visual
description: Make one self-contained HTML page that shows work visually — a recap of what changed on this branch, a plan for a card or task with a flow diagram, or a clickable map of a directory's modules and imports. Use for /factory:visual [recap|plan|<path>], or when the user asks to visualise, picture or visually recap a branch, PR, plan or part of the codebase.
argument-hint: "[recap [--base REF] | plan [#issue|file] | <path>] [--source-base URL]"
---

# Visual — one page that shows the shape of the work

You gather facts into a JSON **spec**; `visual.py render` turns the spec into
the page. The renderer is deterministic and escapes all text, so you never
write HTML. The page is fully inline: no build step, no network, light and
dark mode, readable on a phone. Click any diagram box for its details and
source links.

Tool: `V=${CLAUDE_PLUGIN_ROOT}/scripts/visual.py` (run with `python3`, from
the repo root). The spec format is in the docstring at the top of that file;
read it once before writing a spec.

## Pick the mode

- `recap` → what changed on this branch against its base.
- `plan` → a plan for a card (`#12`), a plan file, or a plan from this
  conversation.
- `<path>` → a map of that directory.
- No argument: branch ahead of base or uncommitted changes → `recap`; a plan
  drafted in this conversation and not built yet → `plan`; otherwise map the
  current directory.

Say the mode and its source in one line, then continue.

## Recap

1. `python3 $V facts [--base REF] > <scratchpad>/spec.json`. This gives you the
   commits, files with `+/-`, a diagram of touched modules (import edges
   between them), risk hints from the paths, and `verify_commands` as tests
   with status `unknown`.
2. Read `git diff <merge-base>` once, in order. Then fill in:
   - `title` (the outcome, not "Changes to X"), `subtitle`, `summary` (what
     changed and why, 1–3 short paragraphs).
   - `highlights`: 3–6 changes a reviewer cares about.
   - `files[].note` for the few files that need a word.
   - Diagram nodes: a `detail` for each node and a `source` like
     `scripts/board.sh:40-72` pointing at the lines that matter. Drop nodes
     that are noise; add untouched neighbours (no `change`) when they explain
     the flow.
   - `risks`: keep the hints that are real, delete the rest, add what the
     paths can't show (behaviour changes, rollout, data).
   - `tests`: run each verify command if it is cheap and set `pass`/`fail`
     from the result; otherwise leave `unknown`. Never report a pass you
     didn't see.
3. Render.

## Plan

Read the card (`gh issue view <n>`) or the plan text, then the real files it
touches. Planning is read-only: make no source edits. Write a spec with
`kind: "plan"`, a `summary` (goal, what done means, non-goals), `steps`
(ordered, each checkable, each naming real `files`; the last step is
verification), `files` to add or change with a `note`, `risks`, and
`questions` with a recommended `default`. Without `diagrams`, the steps are
drawn as a flow. Add a diagram of the target design when the structure
changes, with new pieces marked `"change": "planned"`.

## Map

`python3 $V scan <path> > <scratchpad>/spec.json` lists code files as nodes
(Python, shell, JS/TS) with imports and script calls as edges, and falls back
to one node per subdirectory past `--max-nodes` (default 60). Read the entry
points and enrich it: a `summary`, 3–6 `highlights` (entry points, gotchas,
conventions), a sharper `detail` per node, and node `source` ranges for the
key functions. Set a node's `col` to reorder columns.

## Render

```
python3 $V render <scratchpad>/spec.json [--out FILE] [--source-base URL]
```

It prints `{"out", "bytes"}`. Default output is
`.claude/factory/visual/<slug>.html`, which is ignored by git along with the
rest of `.claude/factory/`. `--source-base https://github.com/OWNER/REPO/blob/<ref>/`
turns every `path:12-20` into a link to `…/path#L12-L20` and commit SHAs into
commit links. Use it when the page will be read away from the checkout.

Reply with the path, the mode, and a one- to three-line gist. Offer to
publish the page as an Artifact (it is fully inline); publish only on a yes.

## Rules

- **Grounded.** Files, line counts and commits come from git through
  `facts`. Your words go in `summary`, `detail`, `highlights` and `risks`;
  mark anything you inferred as inferred.
- **Diagram first.** Every page has at least one diagram of the real
  structure, with `change` set on what moved.
- **Secrets.** Never put token values, `.env` contents or keys in a spec.
  Write `sk-•••` if you have to mention one.
- **Lean.** No filler sections. If a recap is one obvious file, say so and
  skip the page.

## The skill map

`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skill-map.py` renders factory's own
map (skills, scripts, hooks and what uses what) from the files on disk, using
the same renderer. `scripts/build-site.py` puts it on the docs site.
