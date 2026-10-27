#!/usr/bin/env bash
# board.sh — thin, testable wrapper around `gh project` for the factory loop.
#
# Every board read and write the skills make goes through here, so the
# column names, the claim mutex and the rate-limit guard live in one place.
#
# Usage: board.sh <command> [args]
#   doctor                      check gh auth, scopes, config and board columns
#   status                      card counts per column
#   items [column]              JSON array of issue cards (optionally one column)
#   next <column> [n]           up to n unclaimed cards in a column, oldest first
#                               (skips cards held for a human: factory:needs-human)
#   move <issue> <column-key>   move a card (column-key: ready|build|qa|review|done|blocked|backlog)
#   claim <issue>               take the factory:wip label; exits 1 if already held
#   release <issue>             drop the factory:wip label
#   bounces <issue>             how many times QA/Review sent the card back to Build
#   bounce <issue>              record one more bounce (adds factory:bounce-N label)
#   comment <issue> <file|->    post a comment from a file or stdin
#
# Config: $FACTORY_CONFIG, default <repo root>/.claude/factory.json
# Needs: gh (with the `project` scope), jq. Bash 3.2 compatible.

set -euo pipefail

WIP_LABEL="factory:wip"
HOLD_LABEL="factory:needs-human"
BOUNCE_PREFIX="factory:bounce-"

die() { echo "board: $*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

repo_root() { git rev-parse --show-toplevel 2>/dev/null || pwd; }

config_path() { echo "${FACTORY_CONFIG:-$(repo_root)/.claude/factory.json}"; }

cfg() {
  # cfg <jq-path> [default]
  local v
  v=$(jq -r "$1 // empty" "$(config_path)")
  if [ -z "$v" ]; then
    [ $# -ge 2 ] || die "config is missing $1 ($(config_path))"
    v="$2"
  fi
  printf '%s' "$v"
}

column_name() {
  # Map a stable key (build) to the board's display name (Build).
  local key="$1" default
  case "$key" in
    backlog) default="Backlog" ;; ready) default="Ready" ;; build) default="Build" ;;
    qa) default="QA" ;; review) default="Review" ;; done) default="Done" ;;
    blocked) default="Blocked" ;;
    *) die "unknown column key: $key" ;;
  esac
  cfg ".columns.$key" "$default"
}

owner() { cfg .owner; }
project() { cfg .project_number; }
repo() { cfg .repo; }
status_field() { cfg .status_field "Status"; }

# --- rate limit guard --------------------------------------------------------
# ProjectsV2 queries are expensive. If the GraphQL budget is nearly gone,
# sleep until it resets instead of failing a lane halfway through.
rate_guard() {
  local remaining reset now
  remaining=$(gh api rate_limit --jq '.resources.graphql.remaining' 2>/dev/null || echo 9999)
  if [ "$remaining" -lt "${FACTORY_MIN_GRAPHQL:-200}" ]; then
    reset=$(gh api rate_limit --jq '.resources.graphql.reset')
    now=$(date +%s)
    echo "board: GraphQL budget low ($remaining left); sleeping $((reset - now + 5))s" >&2
    sleep $((reset - now + 5))
  fi
}

# --- reads -------------------------------------------------------------------

raw_items() {
  rate_guard
  gh project item-list "$(project)" --owner "$(owner)" --format json --limit 500
}

# Normalised card list: issues only, from the configured repo.
items_json() {
  local field_key repo_url
  # gh exposes a single-select field under its lower-cased name.
  field_key=$(status_field | tr '[:upper:]' '[:lower:]')
  repo_url="https://github.com/$(repo)/"
  raw_items | jq --arg f "$field_key" --arg r "$repo_url" '
    [ .items[]
      | select(.content.type == "Issue")
      | select((.content.url // "") | startswith($r))
      | {
          item_id: .id,
          number: .content.number,
          title: .content.title,
          url: .content.url,
          status: (.[$f] // null),
          labels: (.labels // []),
          assignees: (.assignees // [])
        } ]'
}

cmd_items() {
  if [ $# -ge 1 ]; then
    local col
    col=$(column_name "$1")
    items_json | jq --arg c "$col" '[ .[] | select(.status == $c) ]'
  else
    items_json
  fi
}

cmd_next() {
  [ $# -ge 1 ] || die "usage: next <column> [n]"
  local col n
  col=$(column_name "$1")
  n="${2:-1}"
  items_json | jq --arg c "$col" --arg wip "$WIP_LABEL" --arg hold "$HOLD_LABEL" --argjson n "$n" '
    [ .[] | select(.status == $c)
      | select((.labels | index($wip)) | not)
      | select((.labels | index($hold)) | not) ]
    | sort_by(.number) | .[:$n]'
}

cmd_status() {
  local items key col count
  items=$(items_json)
  for key in backlog ready build qa review blocked "done"; do
    col=$(column_name "$key")
    count=$(jq --arg c "$col" '[ .[] | select(.status == $c) ] | length' <<<"$items")
    printf '%-8s %3s\n' "$col" "$count"
  done
  count=$(jq --arg wip "$WIP_LABEL" '[ .[] | select(.labels | index($wip)) ] | length' <<<"$items")
  printf '%-8s %3s\n' "in-flight" "$count"
}

# --- writes ------------------------------------------------------------------

project_id() {
  gh project view "$(project)" --owner "$(owner)" --format json --jq .id
}

cmd_move() {
  [ $# -eq 2 ] || die "usage: move <issue> <column-key>"
  local issue="$1" col fields field_id option_id item_id
  col=$(column_name "$2")
  rate_guard
  fields=$(gh project field-list "$(project)" --owner "$(owner)" --format json)
  field_id=$(jq -r --arg f "$(status_field)" '.fields[] | select(.name == $f) | .id' <<<"$fields")
  [ -n "$field_id" ] || die "no field named '$(status_field)' on the board"
  option_id=$(jq -r --arg f "$(status_field)" --arg c "$col" \
    '.fields[] | select(.name == $f) | .options[] | select(.name == $c) | .id' <<<"$fields")
  [ -n "$option_id" ] || die "column '$col' does not exist on the board"
  item_id=$(items_json | jq -r --argjson n "$issue" '.[] | select(.number == $n) | .item_id')
  [ -n "$item_id" ] || die "issue #$issue is not on the board"
  gh project item-edit --id "$item_id" --project-id "$(project_id)" \
    --field-id "$field_id" --single-select-option-id "$option_id" >/dev/null
  echo "#$issue -> $col"
}

ensure_label() {
  gh label create "$1" --repo "$(repo)" --color "${2:-5319e7}" \
    --description "${3:-Managed by factory}" >/dev/null 2>&1 || true
}

issue_labels() {
  gh issue view "$1" --repo "$(repo)" --json labels --jq '[.labels[].name]'
}

cmd_claim() {
  [ $# -eq 1 ] || die "usage: claim <issue>"
  ensure_label "$WIP_LABEL" "fbca04" "A factory lane is working on this"
  if issue_labels "$1" | jq -e --arg l "$WIP_LABEL" 'index($l)' >/dev/null; then
    echo "#$1 is already claimed" >&2
    exit 1
  fi
  gh issue edit "$1" --repo "$(repo)" --add-label "$WIP_LABEL" >/dev/null
  echo "claimed #$1"
}

cmd_release() {
  [ $# -eq 1 ] || die "usage: release <issue>"
  gh issue edit "$1" --repo "$(repo)" --remove-label "$WIP_LABEL" >/dev/null 2>&1 || true
  echo "released #$1"
}

cmd_bounces() {
  [ $# -eq 1 ] || die "usage: bounces <issue>"
  issue_labels "$1" | jq --arg p "$BOUNCE_PREFIX" \
    '[ .[] | select(startswith($p)) | ltrimstr($p) | tonumber ] | max // 0'
}

cmd_bounce() {
  [ $# -eq 1 ] || die "usage: bounce <issue>"
  local n
  n=$(cmd_bounces "$1")
  n=$((n + 1))
  ensure_label "${BOUNCE_PREFIX}$n" "d93f0b" "Sent back to Build $n time(s)"
  gh issue edit "$1" --repo "$(repo)" --add-label "${BOUNCE_PREFIX}$n" >/dev/null
  echo "$n"
}

cmd_comment() {
  [ $# -eq 2 ] || die "usage: comment <issue> <file|->"
  if [ "$2" = "-" ]; then
    gh issue comment "$1" --repo "$(repo)" --body-file -
  else
    gh issue comment "$1" --repo "$(repo)" --body-file "$2"
  fi
}

cmd_doctor() {
  local ok=1 scopes fields col key
  need gh; need jq; need git
  [ -f "$(config_path)" ] || die "no config at $(config_path) — run /factory:factory onboard"
  jq empty "$(config_path)" || die "config is not valid JSON"
  if ! gh auth status >/dev/null 2>&1; then
    echo "✗ gh is not signed in (gh auth login)"; exit 1
  fi
  scopes=$(gh auth status 2>&1 | grep -i 'token scopes' || true)
  case "$scopes" in
    *project*) echo "✓ gh signed in with project scope" ;;
    "") echo "? could not read token scopes (fine-grained token?)" ;;
    *) echo "✗ token lacks the project scope: gh auth refresh -s project"; ok=0 ;;
  esac
  if gh repo view "$(repo)" >/dev/null 2>&1; then echo "✓ repo $(repo)"; else echo "✗ cannot see repo $(repo)"; ok=0; fi
  if fields=$(gh project field-list "$(project)" --owner "$(owner)" --format json 2>/dev/null); then
    echo "✓ project $(owner)#$(project)"
    for key in backlog ready build qa review "done" blocked; do
      col=$(column_name "$key")
      if jq -e --arg f "$(status_field)" --arg c "$col" \
          '.fields[] | select(.name == $f) | .options[] | select(.name == $c)' <<<"$fields" >/dev/null; then
        echo "✓ column $col"
      else
        echo "✗ column $col missing from '$(status_field)'"; ok=0
      fi
    done
  else
    echo "✗ cannot read project $(owner)#$(project)"; ok=0
  fi
  if [ "$(jq '.verify_commands | length' "$(config_path)")" -gt 0 ]; then
    echo "✓ verify_commands set"
  else
    echo "✗ verify_commands is empty — the merge gate will refuse to merge"; ok=0
  fi
  if [ "$ok" -eq 1 ]; then echo "ready"; else echo "not ready"; exit 1; fi
}

main() {
  need jq
  [ $# -ge 1 ] || { sed -n '2,20p' "$0"; exit 2; }
  local cmd="$1"; shift
  case "$cmd" in
    doctor) cmd_doctor "$@" ;;
    status) need gh; cmd_status "$@" ;;
    items) need gh; cmd_items "$@" ;;
    next) need gh; cmd_next "$@" ;;
    move) need gh; cmd_move "$@" ;;
    claim) need gh; cmd_claim "$@" ;;
    release) need gh; cmd_release "$@" ;;
    bounces) need gh; cmd_bounces "$@" ;;
    bounce) need gh; cmd_bounce "$@" ;;
    comment) need gh; cmd_comment "$@" ;;
    column) column_name "$@" ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
