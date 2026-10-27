# `/factory:board lint` — tighten cards before they're built

A Build lane can't ask you anything. If a card says "search should work
better", the lane picks a meaning and builds it with confidence. Lint finds
those cards while you're still around to answer, and fixes them **with you**,
one at a time. Nothing is rewritten without your yes.

## 1. Find the problems (deterministic)

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lint-cards.py --json            # Ready
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lint-cards.py --json --backlog  # Ready + Backlog
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lint-cards.py --json --issue 12 # one card
```

Exit 0: every card is clear. Say so in one line and stop. Exit 1: some cards
have problems (the JSON lists them). Exit 2: config or GitHub failed. Show
the message and stop.

The script is the judge of *whether* a card is flagged. Don't add or drop
flags by feel. It gives the same answer every time, so a card you fixed
stays fixed.

| Code | Means | Ask the user for |
|---|---|---|
| `no-criteria` | no checklist, no "Acceptance criteria" section | 2–5 checks that say when it's done |
| `empty-criteria` | the section is there, but empty | same |
| `vague` | a criterion leans on "should work", "better", "properly", "etc"… | the number, message or state that means "better" |
| `not-observable` | nothing a tester could see or measure | what changes on screen, in the API response, in the log |
| `no-repro` | a bug with no steps to reproduce | steps, plus what happens versus what should |
| `multiple-asks` | the title asks for several unrelated things | which one this card is; the rest become new cards |
| `too-big` | more criteria than `lint.max_criteria` (8) | where to split it |

## 2. Show the summary

One line per flagged card, worst first: `#19 Make the chat snappier: vague, not-observable`.
Then: "N cards need tightening. Go through them now?" If no, stop.

## 3. Tighten one card at a time

For each flagged card, in order:

1. Read it in full: `gh issue view <n> --comments`. Look at the code it
   points at, enough to propose something concrete.
2. Say what's wrong in plain words, quoting the exact phrase. "'Snappier'
   doesn't say when it's done." Not "AC #2 is non-deterministic".
3. Propose a rewrite: the checklist you'd want, as `- [ ] <thing you can check>`,
   using real names from the code (routes, messages, commands).
4. If the card is ambiguous in a way you can't settle by reading the code
   (two plausible meanings, a product choice, a missing number), run the
   `mattpocock-skills:grilling` skill on that card: let it ask the user one
   question at a time until the criteria are concrete. Use
   `mattpocock-skills:diagnosing-bugs` instead for a `no-repro` bug, to work
   out the steps with the user. If the skill isn't installed
   (`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/skills-ready.py --skill mattpocock-skills:grilling`
   exits 10), ask the questions yourself, one per message.
5. Offer: **[a]pprove** the rewrite · **[e]dit** it · **[s]plit** the card ·
   **[b]acklog** it (move back with a comment saying what's missing) ·
   **[k]eep** it as is.
6. Only on approve or edit: write it with `gh issue edit <n> --body-file <file>`.
   Keep everything already in the body; replace or add the
   `## Acceptance criteria` section only. Never weaken a criterion that was
   already concrete.
7. On split: create the new cards with `gh issue create` (each with its own
   criteria), add them to the board in Backlog, and link them from the
   original with a comment. The original keeps one ask.
8. Re-run `lint-cards.py --issue <n>` to confirm the card is now clear. If it
   isn't, say what's still flagged and go back to step 3.

The user sets the pace. "Skip" and "stop" are always fine; every change so
far is already saved on GitHub, so a second `lint` picks up where this one
stopped.

## 4. Report

```
lint: 7 cards checked · 3 tightened · 1 split into 2 · 1 back to Backlog · 2 already clear
```

## Rules

- Never edit a card without the user approving that exact text.
- Never mark a criterion done or remove one to make a card pass.
- No local files. Everything lives on the issues.
- `run` lints Ready cards too (step 4 of `run` in SKILL.md), but there it
  only sends unclear cards back to Backlog. The conversation happens here.
