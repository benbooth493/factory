---
name: git-sync
description: Commit the working tree as one commit per logical change, merge-pull, and push without force, then report what happened. Use for /factory:git-sync, or when the user says "sync", "commit and push", "pull and push" or "sync my branch".
---

# /factory:git-sync

Get the user's work committed, caught up and pushed, without losing or
leaking anything. Stop and tell the user whenever a step says stop.

Tool: `S=${CLAUDE_PLUGIN_ROOT}/scripts/git-sync.sh` (run from the repo).
Checker: `CW=${CLAUDE_PLUGIN_ROOT}/scripts/check-writing.py`.

Never: force push (`--force`, `--force-with-lease`, `+refspec`), rebase
published history (unless the user asks), skip hooks (`--no-verify`),
`git add -A` / `git add .` / `git commit -a`, or work around a guard hook
that blocks you. A blocked command means stop and tell the user.

## 1. Look

Run `$S status --fetch`. It prints JSON: `branch`, `upstream`, `ahead`,
`behind`, `in_progress`, `protected`, `staged`, `unstaged`, `untracked`,
`conflicted`, `secrets`, `in_sync`.

- `detached: true` → stop: check out a branch first.
- `in_progress` set (merge, rebase, cherry-pick, revert this run didn't
  start) → stop and say which.
- `in_sync: true` → say "in sync" and stop.
- `protected: true` and there is anything to commit or push → don't commit
  here. Offer to move the work to a branch: suggest a name from the change
  (`fix/upload-size`), and on yes run `$S branch <name>`. It carries the
  uncommitted changes and any unpushed commits over, and resets the
  protected branch to its upstream. On no, stop.

## 2. Commit

1. Read the changes: `git diff`, `git diff --staged`, and each untracked file.
2. Leave out, and list in the report with the reason:
   - every path in `secrets` (secret files by name, or a live-looking key in
     the content). Never stage these; warn the user by name.
   - build output and dependencies (`dist/`, `build/`, `node_modules/`,
     `.venv/`, `*.pyc`, coverage, logs) even if `.gitignore` misses them.
     Suggest a `.gitignore` line instead.
   - anything `git check-ignore -q <path>` says is ignored. Never `git add -f`.
   - files you can't explain from this conversation or the diff (someone
     else's half-finished edit). Ask, don't guess.
3. Group the rest into logical changes: one bug fix, one feature, one
   refactor, one docs update. A change and its tests go together; unrelated
   changes never share a commit. What the user already staged is one group,
   committed as it is.
4. For each group, in an order that keeps every commit working:
   - stage by explicit path: `git add -- <path>...` (`git add -p` is fine for
     splitting a file between groups).
   - write the message to `${CLAUDE_PLUGIN_ROOT}/docs/writing-standard.md`
     (subject: imperative, 72 chars max, no period; blank line; body says
     why). The repo's own documented format wins if it has one. Keep any
     attribution trailer the session asks for.
   - save it to a file in your scratchpad and run `python3 $CW commit <file>`.
     Fix what it reports.
   - `git commit -F <file>`. A failing hook is a stop: report it, never retry
     with `--no-verify`.

## 3. Pull

Run `$S pull`. It merges the upstream (`git pull --no-rebase`), never
rebases. Results:

- `up-to-date`, `no-upstream`, `merged` → go to Push.
- exit 3, `conflict` with `files` → resolve with the
  `mattpocock-skills:resolving-merge-conflicts` skill. Keep both sides'
  intent, hunk by hunk. Never `-X ours/theirs` or `checkout --ours/--theirs`
  on a whole file to make a conflict go away. If a hunk's intent is unclear,
  stop and ask the user, showing both sides. When resolved, run the repo's
  fast checks (`verify_commands` from `.claude/factory.json`, or what its
  `CLAUDE.md` / `CONTRIBUTING.md` documents). If they fail, fix the merge,
  not the checks. Then `git add -- <files>` and `git commit --no-edit`.

## 4. Push

Run `$S push`. It pushes to the upstream, or with `-u origin <branch>` for
a new branch, and refuses (exit 5) on a protected branch or any force.

- exit 4 (rejected, the remote moved) → back to Pull once. Rejected again:
  stop.
- exit 5 → don't look for another way to push. Tell the user why.

If there is no PR for the branch and the user wants one, offer to open it
(`gh pr create --draft`) with a title and body written to the writing
standard, run through the `humanizer` skill, then checked with
`python3 $CW pr <body> --title "<title>"`.

## 5. Report

Short, drop lines that don't apply:

```
Committed  <short sha> <subject>
           <short sha> <subject>
Pulled     <N> commits (no conflicts | conflicts resolved in <files>)
Pushed     <N> commits to <remote>/<branch>
Left out   <path>: <why>
Stopped    <reason and what the user needs to do>
```
