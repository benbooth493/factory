#!/usr/bin/env bash
# refine.sh — plumbing for /factory:ui-refine-loop. Run from the repo root.
#
#   refine.sh detect
#       Find the required skills. Prints JSON:
#       {"impeccable": dir|null, "impeccable_detect": cmd|null,
#        "humanizer": dir|null, "taste_file": path, "missing": [names]}
#       Exit 1 when a required skill is missing (the loop must not start).
#
#   refine.sh open <slug>
#       Worktree .claude/worktrees/refine-<slug> on branch factory/refine-<slug>
#       from HEAD (reused if it exists). Prints JSON:
#       {"worktree", "branch", "run", "base_sha", "base_branch"}.
#       "run" is a scratch dir inside the worktree's git dir: shots and the
#       ledger live there and can never be committed by accident.
#
#   refine.sh table --worktree <wt> --run <run> --slug <slug> --final <label>
#                   [--repo owner/name] [--no-commit]
#       Copy round-0 and <label> shots (<label>-<desktop|mobile>-<light|dark>.png
#       from <run>/shots) to <wt>/docs/factory/refine/<slug>/, commit them, and
#       print a Before | After markdown table whose images are blob URLs pinned
#       to that commit (?raw=true). --repo defaults to the config's repo, then
#       the origin remote.
#
# Exit codes: 0 ok · 1 required skill missing · 2 usage · 3 failed

set -euo pipefail

die() { echo "refine: $1" >&2; exit "${2:-3}"; }

root=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"

cfg() {
  [ -f "$config" ] || return 0
  jq -r "$1 // empty" "$config" 2>/dev/null || true
}

# First <dir>/<name>/SKILL.md found in the project (and its parents), then home,
# then installed plugins.
find_skill() {
  local name="$1" d="$root" home="${HOME:-/nonexistent}" hit
  while :; do
    for sub in .claude/skills .agents/skills; do
      if [ -f "$d/$sub/$name/SKILL.md" ]; then echo "$d/$sub/$name"; return; fi
    done
    [ "$d" = "/" ] && break
    d=$(dirname "$d")
  done
  for sub in .claude/skills .agents/skills; do
    if [ -f "$home/$sub/$name/SKILL.md" ]; then echo "$home/$sub/$name"; return; fi
  done
  if [ -d "$home/.claude/plugins" ]; then
    hit=$(find "$home/.claude/plugins" -maxdepth 7 -path "*/skills/$name/SKILL.md" 2>/dev/null | head -n1)
    if [ -n "$hit" ]; then dirname "$hit"; return; fi
  fi
  return 0
}

cmd_detect() {
  local imp hum detect="" taste missing=()
  imp="${FACTORY_IMPECCABLE_DIR:-$(find_skill impeccable)}"
  hum="${FACTORY_HUMANIZER_DIR:-$(find_skill humanizer)}"
  if [ -n "$imp" ]; then
    # Newer Impeccable ships a launcher; older ones a Node script.
    if [ -x "$imp/scripts/impeccable" ]; then detect="$imp/scripts/impeccable detect --json"
    elif [ -f "$imp/scripts/detect.mjs" ]; then detect="node $imp/scripts/detect.mjs --json"
    fi
  else
    missing+=(impeccable)
  fi
  [ -n "$hum" ] || missing+=(humanizer)
  taste=$(cfg .ui.taste_file)
  [ -n "$taste" ] || taste=".claude/factory/taste.md"
  jq -n --arg imp "$imp" --arg det "$detect" --arg hum "$hum" --arg taste "$taste" \
    --args 'def opt: if . == "" then null else . end;
           {impeccable: ($imp | opt), impeccable_detect: ($det | opt),
            humanizer: ($hum | opt), taste_file: $taste, missing: $ARGS.positional}' \
    ${missing[@]+"${missing[@]}"}
  if [ ${#missing[@]} -gt 0 ]; then
    echo "refine: missing required skill(s): ${missing[*]}" >&2
    return 1
  fi
}

cmd_open() {
  local slug="$1" wt branch base_branch run
  case "$slug" in ''|*[!a-z0-9-]*) die "slug must be lowercase letters, digits and dashes" 2 ;; esac
  wt="$root/.claude/worktrees/refine-$slug"
  branch="factory/refine-$slug"
  base_branch=$(git -C "$root" symbolic-ref --quiet --short HEAD || echo HEAD)
  if [ ! -d "$wt" ]; then
    mkdir -p "$root/.claude/worktrees"
    if git -C "$root" show-ref --quiet "refs/heads/$branch"; then
      git -C "$root" worktree add --quiet "$wt" "$branch" >&2
    else
      git -C "$root" worktree add --quiet -b "$branch" "$wt" HEAD >&2
    fi
  fi
  run="$(git -C "$wt" rev-parse --absolute-git-dir)/factory-refine"
  mkdir -p "$run/shots"
  # The base is recorded once, so a resumed loop still compares to the start.
  [ -s "$run/base" ] || git -C "$root" rev-parse HEAD >"$run/base"
  [ -s "$run/base-branch" ] || echo "$base_branch" >"$run/base-branch"
  jq -n --arg wt "$wt" --arg br "$branch" --arg run "$run" \
    --arg sha "$(cat "$run/base")" --arg bb "$(cat "$run/base-branch")" \
    '{worktree: $wt, branch: $br, run: $run, base_sha: $sha, base_branch: $bb}'
}

repo_from_origin() {
  git -C "$1" remote get-url origin 2>/dev/null \
    | sed -nE 's#^(git@github\.com:|ssh://git@github\.com/|https://([^@/]+@)?github\.com/)([^/]+/[^/]+)$#\3#p' \
    | sed 's/\.git$//'
}

cmd_table() {
  local wt="" run="" slug="" final="" repo="" commit=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --worktree|--run|--slug|--final|--repo)
        [ $# -ge 2 ] || die "$1 needs a value" 2
        case "$1" in
          --worktree) wt="$2" ;; --run) run="$2" ;; --slug) slug="$2" ;;
          --final) final="$2" ;; --repo) repo="$2" ;;
        esac
        shift 2 ;;
      --no-commit) commit=0; shift ;;
      *) die "unknown argument: $1" 2 ;;
    esac
  done
  [ -n "$wt" ] && [ -n "$run" ] && [ -n "$slug" ] && [ -n "$final" ] \
    || die "table needs --worktree --run --slug --final" 2
  [ -n "$repo" ] || repo=$(cfg .repo)
  [ -n "$repo" ] || repo=$(repo_from_origin "$wt")
  [ -n "$repo" ] || die "no repo: set repo in $config or pass --repo owner/name"

  local dest="docs/factory/refine/$slug" copied=0 vp theme side label src
  ls "$run/shots/$final"-*.png >/dev/null 2>&1 || die "no $final shots in $run/shots"
  mkdir -p "$wt/$dest"
  for vp in desktop mobile; do
    for theme in light dark; do
      for side in before after; do
        if [ "$side" = before ]; then label=round-0; else label="$final"; fi
        src="$run/shots/$label-$vp-$theme.png"
        if [ -f "$src" ]; then
          cp "$src" "$wt/$dest/$side-$vp-$theme.png"
          copied=$((copied + 1))
        fi
      done
    done
  done
  [ "$copied" -gt 0 ] || die "no $final shots for desktop/mobile light/dark in $run/shots"

  local after_code
  after_code=$(git -C "$wt" rev-parse --short HEAD)
  if [ "$commit" = 1 ]; then
    git -C "$wt" add -- "$dest"
    if ! git -C "$wt" diff --cached --quiet -- "$dest"; then
      git -C "$wt" commit --quiet -m "ui-refine $slug: before/after screenshots" -- "$dest"
    fi
  fi
  local sha before_code
  sha=$(git -C "$wt" rev-parse HEAD)
  before_code=$(git -C "$wt" rev-parse --short "$(cat "$run/base" 2>/dev/null || echo HEAD)" 2>/dev/null || echo round-0)

  cell() {
    if [ -f "$wt/$dest/$1" ]; then
      printf '<img src="https://github.com/%s/blob/%s/%s/%s?raw=true" width="%s">' "$repo" "$sha" "$dest" "$1" "$2"
    else
      printf 'not shot'
    fi
  }
  echo "| | Before \`$before_code\` | After \`$after_code\` |"
  echo "|---|---|---|"
  for vp in desktop mobile; do
    for theme in light dark; do
      if [ "$vp" = desktop ]; then name="Desktop 1440"; w=420; else name="Mobile 390"; w=200; fi
      echo "| $name $theme | $(cell "before-$vp-$theme.png" "$w") | $(cell "after-$vp-$theme.png" "$w") |"
    done
  done
}

[ $# -ge 1 ] || die "usage: refine.sh detect | open <slug> | table --worktree … --run … --slug … --final …" 2
case "$1" in
  detect) cmd_detect ;;
  open) [ $# -eq 2 ] || die "usage: open <slug>" 2; cmd_open "$2" ;;
  table) shift; cmd_table "$@" ;;
  *) die "unknown command: $1" 2 ;;
esac
