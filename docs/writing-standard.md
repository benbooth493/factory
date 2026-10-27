# Writing standard

One standard for everything factory writes: commit messages, PR titles and
bodies, card (issue) bodies, and lane comments. It applies in factory itself
and in every repo factory works on. If the repo documents its own commit or
PR format (in `CONTRIBUTING.md`, `CLAUDE.md` or `AGENTS.md`), that format
wins where the two disagree; everything else here still applies.

`scripts/check-writing.py` checks the mechanical rules. Run it before you
commit, open a PR, file a card or post a comment:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py commit msg.txt
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py pr body.md --title "Fix the size check on upload"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py card body.md --title "Upload spinner never stops over 10 MB"
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py comment note.md
```

It prints `file:line: problem` for each failure and exits 1. Fix the text,
don't argue with the checker.

## Plain words

- Say what changed and why. Don't restate the card or narrate your work
  ("I have successfully…").
- Short sentences, one idea each. Active voice: "QA found", not "it was found".
- Plain words: "use", not "leverage"; "check", not "validate the integrity of".
- Numbers over adjectives: "1.2 s", not "fast". Evidence over claims: the
  command, the SHA, the `file:line`.
- No hype, no filler, no closing summary line.
- Prose written for humans (PR bodies, card bodies) gets a pass through the
  `humanizer` skill before it is posted. Then run the checker on the result.

Banned phrases (the checker rejects them outside code):
`this pr aims to`, `this commit aims to`, `this change aims to`, `aims to`,
`in order to`, `successfully`, `seamless`, `seamlessly`, `robust`,
`comprehensive`, `leverage`, `utilize`, `delve`, `cutting-edge`,
`game-changer`, `world-class`, `best-in-class`, `state-of-the-art`,
`it's worth noting`, `it is worth noting`, `please note that`, `as an ai`.

## Commit messages

```
Reject uploads over 10 MB before sending them

The server answered 413 and the client never handled it, so the spinner
ran forever. Checking the size first gives the user an error at once and
saves the round trip.

Closes #790
```

- **Subject**: imperative ("Add", "Fix", "Remove", not "Added" or "Adds"),
  72 characters or fewer, no trailing period. A `type(scope): ` prefix is
  fine if the repo uses one; the rules apply to what follows it.
- **Blank line** after the subject.
- **Body** says why: the problem, and anything about the approach a reader
  couldn't get from the diff. Wrap at 72. Skip it only for trivial commits.
- **One logical change per commit.** Unrelated changes go in separate commits.
- **Trailers** last (`Closes #N`, `Co-Authored-By:`), one per line.

## PR title and body

The title follows the commit subject rules. With squash merges it becomes
the commit subject on the base branch.

```
Closes #790

## Why
Uploads over 10 MB spin forever because the client ignores the 413.

## What changed
- Client checks the size before upload and shows "File too large (max 10 MB)"
- One limit constant shared by client and server

## Acceptance criteria
- [x] A 12 MB file shows the size message within 1 s: `e2e/upload-size.spec.ts:14`
- [x] No request over 10 MB is sent: same spec, asserts 0 calls

## Risk
Low. Revert restores the old behaviour; only oversized uploads are affected.
```

| Section | Required | Content |
|---|---|---|
| `Closes #N` | yes | first line; `Refs #N` for partial work |
| `## Why` | yes | the problem in 1–3 sentences |
| `## What changed` | yes | bullets, one change each |
| `## Acceptance criteria` | yes | every criterion from the card, each `- [x]` with the test or file that proves it; `- [ ]` plus a one-line reason if not met |
| `## Risk` | when it applies | what could break, and whether a revert undoes it |
| `## Notes for review` | optional | where to look first, trade-offs taken |

## Card bodies

Title: what is wrong or wanted, in plain words, 80 characters or fewer, no
trailing period, no issue number.

| Section | Required | Content |
|---|---|---|
| `## Problem` | yes | 1–3 sentences from the user's side (`## What is happening` is accepted, as `collect` writes it) |
| `## Context` | optional | where, who, links, screenshots |
| `## Evidence` | bugs | counts, first/last seen, stack frames, logs |
| `## Steps to reproduce` | bugs | numbered, one step per line |
| `## Acceptance criteria` | yes | 1–6 `- [ ]` items, each checkable by running the code |
| `## Out of scope` | optional | what this card deliberately leaves out |

## Lane comments

Short, first line says the outcome, evidence over adjectives. These are the
shapes the lanes post (see `skills/build`, `skills/qa`, `skills/review`):

```
🔨 Build done → QA
PR: #<pr>  head: <short sha>
Criteria → evidence:
- [x] <criterion>: <test or file>
Verify: <commands> ✅
```

```
🧪 QA <PASS|FAIL> at <short sha>
Reason: <bounce reason>            (FAIL only)
- [x|✗] <criterion> — <how verified> → <result>
Checks: <command> ✅|❌
Gaps: <none | list>
```

```
✅ Review approved at <sha>
<2–5 lines on what was checked>
```

Review findings (request changes) are a numbered list, each with
`file:line`, what is wrong, and what would fix it:

```
1. `src/upload.ts:48` — the size check runs after the request starts; move it before `fetch`.
```

A blocked card gets one comment with the specific question and what would
unblock it. Keep any comment to about 8 lines of prose; tables, logs and
screenshots don't count.
