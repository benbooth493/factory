# Screenshots as QA evidence

Any card whose change shows up in a browser gets screenshots, on **pass and
on fail**. They are committed to the PR branch and shown inline in the QA
comment, so a reviewer sees the result without checking anything out.

Tools: `S=${CLAUDE_PLUGIN_ROOT}/scripts/shoot.mjs`,
`C=${CLAUDE_PLUGIN_ROOT}/scripts/serve-check.sh`.

## Does this card touch UI?

Yes if any of these hold:

- the diff (`gh pr diff <pr> --name-only`) touches markup, styles or
  components: `*.html *.css *.scss *.tsx *.jsx *.vue *.svelte *.astro`,
  templates, or anything under the paths in `ui.paths` (config, optional);
- an acceptance criterion talks about what someone sees or clicks (a page,
  button, form, message, layout, colour, mobile view).

If not, write `Screenshots: not applicable (no UI change)` in the report and
skip the rest of this file. Say it, so the omission is visibly deliberate.

## 1. Pick the routes

The route(s) the criteria name, else the page(s) that render the changed
components (grep for the import). One to three routes; more means the card
is too big, and you note it as a gap.

## 2. Get the app running

From the worktree, at the pinned commit:

```bash
bash $C            # JSON: reachable, base_url, start_command
bash $C start      # if not reachable: starts ui.start_command, waits for it
```

Settings live in `.claude/factory.json` under `ui` (`base_url`, default
`http://localhost:3000`; `start_command`, default the `dev` or `start`
script; `ready_path`, default `/`). Install dependencies first if the
worktree has none. Stop what you started when done (`kill <pid>` from the
JSON).

If the app can't be started, or `shoot.mjs` exits 3 (no Playwright in the
project), the UI criteria have no evidence: that is a **fail** with
`Reason: environment`, and the comment says exactly what is missing (for
example "set `ui.start_command`" or "`npm i -D playwright && npx playwright
install chromium`"). Never pass a UI card on reading the code alone.

## 3. Shoot

```bash
node $S --base <base_url> --route <route> --out .factory-shots --label <route-slug>
```

That gives full pages at **desktop 1920×1080, tablet 1024×768 and mobile
375×667**, waits for the network to go idle, and prints a JSON manifest.
Add `--themes light,dark` when the app has a dark mode, and
`--select '<css>'` to crop the element a criterion is about. Read every
image yourself: a sign-in page, an error overlay or a blank page is not
evidence of anything except a broken setup.

On a fail, these shots show the broken state. That is the point: Build sees
what you saw.

## 4. Commit the shots before you comment

Images in a comment only render once the file is on GitHub, so the commit
and push come first.

```bash
dir=docs/factory/runs/<issue>/qa-<short sha>     # <sha> = the commit you tested
git checkout -B <headRefName> <sha>              # you were detached at <sha>
mkdir -p "$dir" && mv .factory-shots/*.png "$dir"/ && rm -rf .factory-shots
git add "$dir" && git commit -m "QA evidence for #<issue> at <short sha>"
git push origin <headRefName>                    # plain push, never --force
shot_sha=$(git rev-parse HEAD)
```

- If the push is rejected, the branch moved under you: the code you tested
  is no longer the head. Stop, discard your commit, and report that instead
  of a verdict.
- The evidence commit adds files under `docs/factory/runs/` only. The
  verdict is still about `<sha>`, and you say so in the report. Review
  approves the new head, after checking it differs from `<sha>` only there.
- Images over 5 MB render badly. Use crops (`--select`) or a narrower page
  rather than committing huge files.

## 5. Embed them

Link each image with the **blob URL pinned to `shot_sha`**, plus `?raw=true`.
That works for private repos (the viewer's own GitHub session fetches it)
and keeps working after the branch is deleted on merge. Never link a branch
name, and never upload shots to an outside image host.

```
https://github.com/<owner>/<repo>/blob/<shot_sha>/docs/factory/runs/<issue>/qa-<short sha>/<file>.png?raw=true
```

`<owner>/<repo>` is `repo` from the config. Put this block in the QA
report, after the criteria lines, in both the PR comment and the issue
comment:

```markdown
Screenshots (`<route>` at <short sha>):

| Desktop 1920×1080 | Tablet 1024×768 | Mobile 375×667 |
|---|---|---|
| <img src="…-desktop-light.png?raw=true" width="320"> | <img src="…-tablet-light.png?raw=true" width="240"> | <img src="…-mobile-light.png?raw=true" width="140"> |
```

One row per route; add a dark row when you shot dark. Crops go under the
table as `![<what it shows>](<url>)` with a few words each. On a fail, put
one line under the table saying what in the shot is wrong.

## Checklist

- [ ] UI decision stated (shots, or "not applicable" with the reason)
- [ ] every UI criterion has a shot that shows it
- [ ] shots committed and pushed **before** the comment
- [ ] links pinned to the evidence commit SHA, `?raw=true`
- [ ] same block on the PR comment and the issue comment
