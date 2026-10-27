#!/usr/bin/env bash
# board-migrate.sh — bring an existing board's Status field into factory's shape.
#
# Usage: board-migrate.sh [--dry-run]
#
#   1. adds any missing Status option (Backlog, Ready, Build, QA, Review,
#      Blocked, Done, or the names set in config `columns`)
#   2. moves cards out of legacy columns into factory's:
#        Building, In progress, Doing, WIP  -> Build
#        Todo, To do, Up next              -> Ready
#        In review, Code review            -> Review
#        Testing, In QA                    -> QA
#        Icebox, Ideas                     -> Backlog
#        Complete, Completed, Shipped      -> Done
#      (add your own with config `board_migrate.aliases`: {"Parked": "blocked"})
#   3. never deletes an option. Emptied legacy columns are left for you to
#      remove in the GitHub UI once you're happy.
#
# Adding options rewrites the whole single-select field (GitHub has no "add one
# option" mutation), which can clear every card's Status. So every card's
# column is read first, and after the rewrite each card is put back where it
# was (or where its legacy column maps to) and read back to check.
#
# --dry-run prints the plan and changes nothing.
# Exit codes: 0 done or nothing to do, 1 a GitHub call failed or a check
# didn't hold, 2 bad usage or config.
#
# Config: $FACTORY_CONFIG, default <repo root>/.claude/factory.json.
# Needs gh (project scope) and jq. Bash 3.2 compatible.

# shellcheck disable=SC2016  # $vars in GraphQL and jq programs are theirs, not the shell's

set -euo pipefail

die() { echo "board-migrate: $*" >&2; exit "${2:-1}"; }

dry=0
case "${1:-}" in
  --dry-run) dry=1 ;;
  "") ;;
  *) die "usage: board-migrate.sh [--dry-run]" 2 ;;
esac

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
config="${FACTORY_CONFIG:-$root/.claude/factory.json}"
[ -f "$config" ] || die "no config at $config" 2
owner=$(jq -r '.owner // empty' "$config"); project=$(jq -r '.project_number // empty' "$config")
[ -n "$owner" ] && [ -n "$project" ] || die "config needs owner and project_number" 2
field=$(jq -r '.status_field // "Status"' "$config")

# Factory's columns, in board order: [{key, name, color}]
columns=$(jq -c '(.columns // {}) as $c |
  [ ["backlog","Backlog","GRAY"], ["ready","Ready","BLUE"], ["build","Build","YELLOW"],
    ["qa","QA","ORANGE"], ["review","Review","PURPLE"], ["blocked","Blocked","RED"], ["done","Done","GREEN"] ]
  | map({key: .[0], name: ($c[.[0]] // .[1]), color: .[2]})' "$config")

# Legacy name (lower-cased) -> column key. Config aliases win.
aliases=$(jq -c '{
  "building":"build", "in progress":"build", "doing":"build", "wip":"build", "in development":"build",
  "todo":"ready", "to do":"ready", "up next":"ready",
  "in review":"review", "code review":"review", "reviewing":"review",
  "testing":"qa", "in qa":"qa",
  "icebox":"backlog", "ideas":"backlog",
  "complete":"done", "completed":"done", "shipped":"done"
} + ((.board_migrate.aliases // {}) | with_entries(.key |= ascii_downcase))' "$config")

fields=$(gh project field-list "$project" --owner "$owner" --format json) || die "cannot read project $owner#$project"
field_id=$(jq -r --arg f "$field" '.fields[] | select(.name == $f) | .id' <<<"$fields")
[ -n "$field_id" ] || die "no single-select field named '$field' on $owner#$project"

options_query='query($id:ID!){node(id:$id){... on ProjectV2SingleSelectField{options{id name color description}}}}'
read_options() {
  jq -n --arg q "$options_query" --arg id "$field_id" '{query:$q, variables:{id:$id}}' \
    | gh api graphql --input - --jq '.data.node.options'
}
options=$(read_options) || die "cannot read the options of '$field'"
[ "$(jq 'length' <<<"$options")" -gt 0 ] || die "'$field' has no options"

# Every card on the board (issues, PRs and drafts alike), with its column.
field_key=$(printf '%s' "$field" | tr '[:upper:]' '[:lower:]')
read_items() {
  gh project item-list "$project" --owner "$owner" --format json --limit 1000 \
    | jq -c --arg f "$field_key" '[.items[] | {id, status: (.[$f] // null),
        label: (if .content.number then "#\(.content.number)" else (.content.title // .id) end)}]'
}
items=$(read_items) || die "cannot read the board's cards"

# The plan, worked out in one place.
plan=$(jq -cn --argjson cols "$columns" --argjson opts "$options" --argjson al "$aliases" --argjson items "$items" '
  ($opts | map(.name | ascii_downcase)) as $have
  | ($cols | map({(.key): .name}) | add) as $name
  | ($cols | map({(.name | ascii_downcase): .name}) | add) as $own
  | {
      missing: [ $cols[] | select((.name | ascii_downcase) as $n | $have | index($n) | not) ],
      desired: ( [ $cols[] as $c
                   | ($opts | map(select((.name | ascii_downcase) == ($c.name | ascii_downcase))) | first)
                       // {name: $c.name, color: $c.color, description: ""} ]
               + [ $opts[] | select((.name | ascii_downcase) as $n | $own[$n] == null) ] ),
      items: [ $items[] | . + {want: (
                 if .status == null then null
                 else (.status | ascii_downcase) as $s
                   | if $own[$s] then $own[$s]
                     elif $al[$s] then $name[$al[$s]]
                     else .status end
                 end) } ],
    }
  | .moves = [ .items[] | select(.status != null and .want != .status) ]
  | .unknown = ([ .items[] | select(.status != null and .want == .status
                   and ($own[.status | ascii_downcase] == null)) | .status ] | group_by(.)
                 | map({name: .[0], cards: length}))')

missing=$(jq -r '.missing | map(.name) | join(", ")' <<<"$plan")
nmoves=$(jq '.moves | length' <<<"$plan")

echo "board-migrate: $owner#$project, field '$field'"
if [ -n "$missing" ]; then echo "  add columns: $missing"; else echo "  columns: all present"; fi
jq -r '.moves | group_by(.status + " -> " + .want)[] | "  move \(length) card(s) \(.[0].status) -> \(.[0].want): \(map(.label) | join(" "))"' <<<"$plan"
jq -r '.unknown[] | "  leave \(.cards) card(s) in \(.name) (not a factory column; map it with board_migrate.aliases)"' <<<"$plan"
[ -n "$missing" ] || [ "$nmoves" -gt 0 ] || { echo "  nothing to do"; exit 0; }
if [ "$dry" -eq 1 ]; then echo "  dry run: nothing changed"; exit 0; fi

project_id=$(gh project view "$project" --owner "$owner" --format json --jq .id) || die "cannot read the project id"

# 1. Add missing options. Keep every existing option (so none that holds a
#    card is ever deleted), in factory's order, extras after.
if [ -n "$missing" ]; then
  # A copy of every card's column, in case anything below goes wrong.
  backup_dir="$root/.claude/factory"
  mkdir -p "$backup_dir"
  backup="$backup_dir/board-backup-$project-$(date +%Y%m%d%H%M%S).json"
  jq '[.[] | {id, label, status}]' <<<"$items" > "$backup"
  echo "  saved every card's column to $backup"

  mutation='mutation($id:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){updateProjectV2Field(input:{fieldId:$id,singleSelectOptions:$opts}){projectV2Field{... on ProjectV2SingleSelectField{options{id name}}}}}'
  jq -n --arg q "$mutation" --arg id "$field_id" --argjson plan "$plan" \
    '{query:$q, variables:{id:$id, opts:[$plan.desired[] | {name, color, description: (.description // "")}]}}' \
    | gh api graphql --input - >/dev/null || die "could not update the options of '$field'; cards' columns are in $backup"
  options=$(read_options) || die "cannot re-read the options"
  n=$(jq '.missing | length' <<<"$plan"); i=0
  while [ "$i" -lt "$n" ]; do
    name=$(jq -r --argjson i "$i" '.missing[$i].name' <<<"$plan"); i=$((i + 1))
    jq -e --arg n "$name" 'map(.name) | index($n)' <<<"$options" >/dev/null \
      || die "column '$name' is still missing after the update; cards' columns are in $backup"
    echo "  added $name"
  done
fi

# 2. Put every card in its column: the mapped one for legacy cards, its own
#    for the rest (in case the option rewrite cleared it). Read back after.
now=$(read_items) || die "cannot re-read the board's cards"
fixes=$(jq -c --argjson plan "$plan" --argjson opts "$options" '
  (map({(.id): .status}) | add // {}) as $cur
  | [ $plan.items[] | select(.want != null and $cur[.id] != .want)
      | . as $i | . + {option: ($opts | map(select(.name == $i.want)) | first | .id)} ]' <<<"$now")
moved=0
n=$(jq 'length' <<<"$fixes"); i=0
while [ "$i" -lt "$n" ]; do
  row=$(jq -c --argjson i "$i" '.[$i]' <<<"$fixes"); i=$((i + 1))
  id=$(jq -r .id <<<"$row"); want=$(jq -r .want <<<"$row"); opt=$(jq -r '.option // empty' <<<"$row")
  [ -n "$opt" ] || die "no option '$want' for $(jq -r .label <<<"$row")"
  gh project item-edit --id "$id" --project-id "$project_id" --field-id "$field_id" \
    --single-select-option-id "$opt" >/dev/null || die "could not move $(jq -r .label <<<"$row") to $want"
  moved=$((moved + 1))
done

final=$(read_items) || die "cannot read the board back"
wrong=$(jq -r --argjson plan "$plan" '(map({(.id): .status}) | add // {}) as $cur
  | [ $plan.items[] | select(.want != null and $cur[.id] != .want) | "\(.label) is in \($cur[.id] // "no column"), expected \(.want)" ]
  | .[]' <<<"$final")
[ -z "$wrong" ] || die "after migrating: $wrong"
echo "  done: $moved card(s) placed; every card reads back in the right column"
