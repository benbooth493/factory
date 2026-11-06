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
#   next <col>[,<col>...] [n]   up to n unclaimed cards, by column order then oldest
#                               first, from one board read (skips factory:needs-human)
#   move <issue> <column-key>   move a card (column-key: ready|build|qa|review|done|blocked|backlog)
#   claim <issue>               take the factory:wip label; exits 1 if already held
#   release <issue>             drop the factory:wip label
#   bounces <issue>             how many times QA/Review sent the card back to Build
#   bounce <issue>              record one more bounce (adds factory:bounce-N label)
#   comment <issue> <file|->    post a comment from a file or stdin
#
# Config: $FACTORY_CONFIG, default <repo root>/.claude/factory.json
# Needs: gh (with the `project` scope), jq. Bash 3.2 compatible.

# shellcheck disable=SC2016  # $vars in GraphQL and jq programs are theirs, not the shell's

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

# Run gh; on failure name a rate-limit refusal plainly. GitHub's secondary
# limit doesn't show in `gh api rate_limit`, so rate_guard can't see it coming.
ghq() {
  local err rc=0
  err=$(mktemp "${TMPDIR:-/tmp}/board-gh.XXXXXX")
  gh "$@" 2>"$err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -qiE 'rate limit|too quickly' "$err"; then
      echo "board: GitHub refused with a rate limit: $(head -n1 "$err")" >&2
      echo "board: this is GitHub's secondary rate limit (gh api rate_limit doesn't show it); wait 15-30 minutes, retrying sooner makes it longer" >&2
    else
      cat "$err" >&2
    fi
    rm -f "$err"
    exit "$rc"
  fi
  rm -f "$err"
}

# --- reads -------------------------------------------------------------------

# Paged, and only the fields board.sh uses: item-list fetches every field of
# every item and trips the secondary rate limit on boards with a few hundred cards.
ITEMS_QUERY='query($login: String!, $number: Int!, $status: String!, $endCursor: String) {
  repositoryOwner(login: $login) { ... on ProjectV2Owner { projectV2(number: $number) {
    items(first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id isArchived
        fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
        content { ... on Issue { number title url state labels(first: 50) { nodes { name } } } }
      } } } } } }'

# One JSON node per line.
raw_items() {
  rate_guard
  ghq api graphql --paginate -f query="$ITEMS_QUERY" -f login="$(owner)" \
    -F number="$(project)" -f status="$(status_field)" \
    --jq '.data.repositoryOwner.projectV2.items.nodes[]'
}

# Normalised card list: unarchived issues from the configured repo.
items_json() {
  local raw
  raw=$(raw_items) || exit 1
  jq -s --arg r "https://github.com/$(repo)/" '
    [ .[]
      | select(.isArchived | not)
      | select((.content.url // "") | startswith($r))
      | {
          item_id: .id,
          number: .content.number,
          title: .content.title,
          url: .content.url,
          state: .content.state,
          status: (.fieldValueByName.name // null),
          labels: [.content.labels.nodes[]?.name]
        } ]' <<<"$raw"
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
  [ $# -ge 1 ] || die "usage: next <column>[,<column>...] [n]"
  local cols="[]" key col n
  n="${2:-1}"
  for key in $(printf '%s' "$1" | tr ',' ' '); do
    col=$(column_name "$key")
    cols=$(jq -c --arg c "$col" '. + [$c]' <<<"$cols")
  done
  items_json | jq --argjson cols "$cols" --arg wip "$WIP_LABEL" --arg hold "$HOLD_LABEL" --argjson n "$n" '
    [ .[] | select(.status as $s | $cols | index($s))
      | select((.labels | index($wip)) | not)
      | select((.labels | index($hold)) | not) ]
    | sort_by(.status as $s | ($cols | index($s)), .number) | .[:$n]'
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

# The card's own project items: finds the item, project and Status options
# without listing the board.
CARD_QUERY='query($owner: String!, $name: String!, $number: Int!, $status: String!) {
  repository(owner: $owner, name: $name) { issue(number: $number) {
    projectItems(first: 20) { nodes {
      id
      project { id number url
        field(name: $status) { ... on ProjectV2SingleSelectField { id options { id name } } } }
    } } } } }'

cmd_move() {
  [ $# -eq 2 ] || die "usage: move <issue> <column-key>"
  local issue="$1" col r card item_id project_id field_id option_id
  col=$(column_name "$2")
  r=$(repo)
  rate_guard
  card=$(ghq api graphql -f query="$CARD_QUERY" -f owner="${r%%/*}" -f name="${r#*/}" \
    -F number="$issue" -f status="$(status_field)")
  card=$(jq -c --argjson p "$(project)" --arg o "$(owner)" '
    [ .data.repository.issue.projectItems.nodes[]
      | select(.project.number == $p)
      | select(.project.url | ascii_downcase | endswith("/\($o | ascii_downcase)/projects/\($p)")) ]
    | first // empty' <<<"$card")
  [ -n "$card" ] || die "issue #$issue is not on the board"
  item_id=$(jq -r .id <<<"$card")
  project_id=$(jq -r .project.id <<<"$card")
  field_id=$(jq -r '.project.field.id // empty' <<<"$card")
  [ -n "$field_id" ] || die "no field named '$(status_field)' on the board"
  option_id=$(jq -r --arg c "$col" '.project.field.options[] | select(.name == $c) | .id' <<<"$card")
  [ -n "$option_id" ] || die "column '$col' does not exist on the board"
  ghq project item-edit --id "$item_id" --project-id "$project_id" \
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
