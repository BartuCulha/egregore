#!/bin/bash
set -euo pipefail

# Granola meeting data reader
# Two modes: API (live, always fresh) and cache (offline fallback)
#
# API mode: uses WorkOS tokens from Granola's local supabase.json
# Cache mode: reads from Granola's local cache-vN.json file
#
# Data model (both modes produce the same output shape):
#   documents: { id, title, created_at, people, notes_markdown, ... }
#   folders:   { id, title, document_ids, ... }
#   transcripts: [{ text, source, start_timestamp, end_timestamp }]

CACHE_DIR="$HOME/Library/Application Support/Granola"
SUPABASE_FILE="$CACHE_DIR/supabase.json"
GRANOLA_API="https://api.granola.ai"

# Auto-detect highest cache version (cache-vN.json)
CACHE_FILE=$(ls -1 "$CACHE_DIR"/cache-v*.json 2>/dev/null | sort -t'v' -k2 -n | tail -1)
CACHE_FILE="${CACHE_FILE:-$CACHE_DIR/cache-v3.json}"  # fallback for clear error in check_cache

# --- Auth helpers ---

get_workos_token() {
  if [ ! -f "$SUPABASE_FILE" ]; then
    echo ""
    return
  fi
  local tokens
  tokens=$(jq -r '.workos_tokens // empty' "$SUPABASE_FILE")
  if [ -z "$tokens" ]; then
    echo ""
    return
  fi

  local access_token obtained_at expires_in now
  access_token=$(echo "$tokens" | jq -r '.access_token // empty')
  obtained_at=$(echo "$tokens" | jq -r '.obtained_at // 0')
  expires_in=$(echo "$tokens" | jq -r '.expires_in // 0')
  now=$(date +%s)

  # Convert obtained_at from ms to seconds
  local obtained_at_sec=$(( obtained_at / 1000 ))
  local expires_at=$(( obtained_at_sec + expires_in ))

  if [ "$now" -lt "$expires_at" ]; then
    echo "$access_token"
    return
  fi

  # Token expired — try to refresh
  local refresh_token client_id
  refresh_token=$(echo "$tokens" | jq -r '.refresh_token // empty')
  # Extract client_id from the JWT (second segment, base64-decode, parse JSON)
  client_id=$(echo "$access_token" | cut -d'.' -f2 | base64 -d 2>/dev/null | jq -r '.iss // empty' | sed 's|.*/||')

  if [ -z "$refresh_token" ] || [ -z "$client_id" ]; then
    echo ""
    return
  fi

  # Refresh the token via WorkOS
  local resp
  resp=$(curl -s -X POST "https://api.workos.com/user_management/authenticate" \
    -H "Content-Type: application/json" \
    -d "{\"client_id\":\"$client_id\",\"grant_type\":\"refresh_token\",\"refresh_token\":\"$refresh_token\"}" 2>/dev/null) || true

  local new_access new_refresh
  new_access=$(echo "$resp" | jq -r '.access_token // empty' 2>/dev/null)

  if [ -z "$new_access" ]; then
    echo ""
    return
  fi

  new_refresh=$(echo "$resp" | jq -r '.refresh_token // empty' 2>/dev/null)
  local new_expires_in=$(echo "$resp" | jq -r '.expires_in // 21600' 2>/dev/null)
  local new_obtained_at=$(( $(date +%s) * 1000 ))

  # Update supabase.json with new tokens (rotation — refresh tokens are single-use)
  local session_id external_id sign_in_method
  session_id=$(echo "$tokens" | jq -r '.session_id // empty')
  external_id=$(echo "$tokens" | jq -r '.external_id // empty')
  sign_in_method=$(echo "$tokens" | jq -r '.sign_in_method // empty')

  local new_tokens
  new_tokens=$(jq -n \
    --arg at "$new_access" \
    --arg rt "${new_refresh:-$refresh_token}" \
    --argjson ei "$new_expires_in" \
    --argjson oa "$new_obtained_at" \
    --arg sid "$session_id" \
    --arg eid "$external_id" \
    --arg sim "$sign_in_method" \
    '{access_token:$at,refresh_token:$rt,expires_in:$ei,obtained_at:$oa,token_type:"Bearer",session_id:$sid,external_id:$eid,sign_in_method:$sim}')

  # Write back (atomic via tmp file)
  local tmp_file="$SUPABASE_FILE.tmp.$$"
  jq --arg wt "$new_tokens" '.workos_tokens = $wt' "$SUPABASE_FILE" > "$tmp_file" && mv "$tmp_file" "$SUPABASE_FILE"

  echo "$new_access"
}

api_call() {
  local endpoint="$1"
  local body="${2:-{\}}"
  local token
  token=$(get_workos_token)

  if [ -z "$token" ]; then
    echo '{"error":"no_auth"}' >&2
    return 1
  fi

  curl -s --compressed -X POST "${GRANOLA_API}${endpoint}" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -H "User-Agent: Granola/5.354.0" \
    -H "X-Client-Version: 5.354.0" \
    -d "$body"
}

api_available() {
  local token
  token=$(get_workos_token)
  [ -n "$token" ]
}

# --- Cache helpers ---

check_cache() {
  if [ ! -d "$CACHE_DIR" ]; then
    echo "Granola not found — $CACHE_DIR does not exist." >&2
    exit 1
  fi
  if [ ! -f "$CACHE_FILE" ]; then
    echo "Granola cache not found — $CACHE_FILE does not exist." >&2
    exit 1
  fi
}

get_state() {
  jq -r '.cache' "$CACHE_FILE" | jq '.state'
}

# --- Subcommands ---

cmd_test() {
  local api_ok=false
  local cache_ok=false

  if api_available; then
    local resp
    resp=$(api_call "/v2/get-documents" '{"limit":1,"offset":0}' 2>/dev/null) || true
    if echo "$resp" | jq -e '.docs' >/dev/null 2>&1; then
      api_ok=true
      echo "Granola API: connected"
    else
      echo "Granola API: auth failed (token may need refresh)"
    fi
  else
    echo "Granola API: no auth tokens found"
  fi

  if [ -f "$CACHE_FILE" ]; then
    cache_ok=true
    local doc_count
    doc_count=$(get_state | jq '.documents | length')
    local cache_age
    cache_age=$(( ( $(date +%s) - $(stat -f %m "$CACHE_FILE") ) / 3600 ))
    echo "Granola cache: $doc_count documents (${cache_age}h old) — $(basename "$CACHE_FILE")"
  else
    echo "Granola cache: not found"
  fi

  if ! $api_ok && ! $cache_ok; then
    echo "No Granola data source available." >&2
    exit 1
  fi
}

cmd_folders() {
  if api_available; then
    local resp
    resp=$(api_call "/v2/get-document-lists" '{}' 2>/dev/null) || true
    if echo "$resp" | jq -e '.[0].id' >/dev/null 2>&1; then
      echo "$resp" | jq '[.[] | select(.deleted_at == null) | {
        id: .id,
        name: .title,
        doc_count: (.document_ids // [] | length)
      }]'
      return
    fi
  fi

  # Fallback to cache
  check_cache
  local state
  state=$(get_state)
  echo "$state" | jq '
    .documentLists as $lists |
    [.documentListsMetadata | to_entries[] | .value |
      select(.deleted_at == null) |
      {
        id: .id,
        name: .title,
        doc_count: ($lists[.id] // [] | length)
      }
    ]
  '
}

cmd_list() {
  local folder=""
  local since=""
  local exclude=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --folder)
        folder="$2"
        shift 2
        ;;
      --since)
        since="$2"
        shift 2
        ;;
      --exclude)
        exclude="$2"
        shift 2
        ;;
      *)
        echo "Unknown option: $1" >&2
        exit 1
        ;;
    esac
  done

  local result=""

  # Try API first
  if api_available; then
    if [ -n "$folder" ]; then
      # Get folder doc IDs, then batch-fetch docs
      local folders_resp
      folders_resp=$(api_call "/v2/get-document-lists" '{}' 2>/dev/null) || true
      if echo "$folders_resp" | jq -e '.[0].id' >/dev/null 2>&1; then
        local doc_ids_json
        doc_ids_json=$(echo "$folders_resp" | jq -c --arg fname "$folder" '
          [.[] | select(.title == $fname) | (.document_ids // [])[]]
        ')
        local id_count
        id_count=$(echo "$doc_ids_json" | jq 'length')
        if [ "$id_count" -gt 0 ]; then
          local batch_resp
          batch_resp=$(api_call "/v1/get-documents-batch" "{\"document_ids\":$doc_ids_json,\"include_last_viewed_panel\":false}" 2>/dev/null) || true
          if echo "$batch_resp" | jq -e '.docs // .documents' >/dev/null 2>&1; then
            result=$(echo "$batch_resp" | jq '[(.docs // .documents)[] |
              select(.deleted_at == null) |
              {
                id: .id,
                title: .title,
                date: (.created_at // .meeting_starts_at),
                attendees: [(.people.attendees // [])[] | .details.person.name.fullName // .email // "unknown"]
              }
            ]')
          fi
        else
          result="[]"
        fi
      fi
    else
      # List all documents
      local resp
      resp=$(api_call "/v2/get-documents" '{"limit":200,"offset":0}' 2>/dev/null) || true
      if echo "$resp" | jq -e '.docs' >/dev/null 2>&1; then
        result=$(echo "$resp" | jq '[.docs[] |
          select(.deleted_at == null) |
          {
            id: .id,
            title: .title,
            date: (.created_at // .meeting_starts_at),
            attendees: [(.people.attendees // [])[] | .details.person.name.fullName // .email // "unknown"]
          }
        ]')
      fi
    fi
  fi

  # Fallback to cache if API didn't work
  if [ -z "$result" ]; then
    check_cache
    local state
    state=$(get_state)

    if [ -n "$folder" ]; then
      result=$(echo "$state" | jq --arg fname "$folder" '
        .documentLists as $lists |
        .documentListsMetadata as $meta |
        .documents as $docs |
        (
          [$meta | to_entries[] | select(.value.title == $fname) | .key] | .[0]
        ) as $folder_id |
        (if $folder_id then $lists[$folder_id] // [] else [] end) as $member_ids |
        [
          $member_ids[] |
          . as $did |
          $docs[$did] // empty |
          select(.deleted_at == null) |
          {
            id: .id,
            title: .title,
            date: .created_at,
            attendees: [(.people.attendees // [])[] | .details.person.name.fullName // .email]
          }
        ]
      ')
    else
      result=$(echo "$state" | jq '
        [.documents | to_entries[] | .value |
          select(.deleted_at == null) |
          {
            id: .id,
            title: .title,
            date: .created_at,
            attendees: [(.people.attendees // [])[] | .details.person.name.fullName // .email]
          }
        ]
      ')
    fi
  fi

  # Apply --since filter
  if [ -n "$since" ]; then
    result=$(echo "$result" | jq --arg since "$since" '
      [.[] | select(.date >= $since)]
    ')
  fi

  # Apply --exclude filter (comma-separated doc IDs)
  if [ -n "$exclude" ]; then
    result=$(echo "$result" | jq --arg exclude "$exclude" '
      ($exclude | split(",")) as $excluded |
      [.[] | select(.id as $mid | $excluded | any(. == $mid) | not)]
    ')
  fi

  # Sort by date descending
  echo "$result" | jq 'sort_by(.date) | reverse'
}

cmd_get() {
  local doc_id="${1:-}"
  if [ -z "$doc_id" ]; then
    echo "Usage: granola.sh get <doc-id>" >&2
    exit 1
  fi

  # Try API first
  if api_available; then
    local batch_resp transcript_resp
    batch_resp=$(api_call "/v1/get-documents-batch" "{\"document_ids\":[\"$doc_id\"],\"include_last_viewed_panel\":true}" 2>/dev/null) || true

    if echo "$batch_resp" | jq -e '(.docs // .documents)[0]' >/dev/null 2>&1; then
      local doc
      doc=$(echo "$batch_resp" | jq '(.docs // .documents)[0]')

      # Get transcript
      transcript_resp=$(api_call "/v1/get-document-transcript" "{\"document_id\":\"$doc_id\"}" 2>/dev/null) || true

      local panel_text=""
      # Try notes_markdown first
      panel_text=$(echo "$doc" | jq -r '.notes_markdown // empty')

      # Fall back to ProseMirror panel content → convert to markdown
      if [ -z "$panel_text" ]; then
        panel_text=$(echo "$doc" | jq -r '
          def pm_to_md:
            if type != "object" then ""
            elif .type == "text" then (.text // "")
            elif .type == "heading" then
              ("#" * (.attrs.level // 1)) + " " + ([.content[]? | pm_to_md] | join("")) + "\n\n"
            elif .type == "paragraph" then
              ([.content[]? | pm_to_md] | join("")) + "\n\n"
            elif .type == "bulletList" then
              ([.content[]? | pm_to_md] | join(""))
            elif .type == "listItem" then
              "- " + ([.content[]? | pm_to_md] | join("") | ltrimstr("- "))
            elif .type == "doc" then
              ([.content[]? | pm_to_md] | join(""))
            else
              ([.content[]? | pm_to_md] | join(""))
            end;
          .last_viewed_panel.content // empty | pm_to_md
        ' 2>/dev/null || echo "")
      fi

      local transcript_text=""
      local transcript_structured="[]"
      if echo "$transcript_resp" | jq -e '.[0]' >/dev/null 2>&1; then
        transcript_text=$(echo "$transcript_resp" | jq -r '[.[] | .text // empty] | join("\n")' 2>/dev/null || echo "")
        transcript_structured=$(echo "$transcript_resp" | jq '[.[] | {
          text: (.text // ""),
          source: (.source // "unknown"),
          start: (.start_timestamp // null),
          end: (.end_timestamp // null)
        }]' 2>/dev/null || echo "[]")
      fi

      echo "$doc" | jq \
        --arg panel "$panel_text" \
        --arg transcript "$transcript_text" \
        --argjson transcript_structured "$transcript_structured" \
        '{
          id: .id,
          title: .title,
          date: (.created_at // .meeting_starts_at),
          attendees: [
            ((.people.creator // {}) | {name: (.details.person.name.fullName // .name // "unknown"), email: (.email // "")}),
            ((.people.attendees // [])[] | {name: (.details.person.name.fullName // .email), email: (.email // "")})
          ],
          panel_text: $panel,
          transcript_text: $transcript,
          transcript_structured: $transcript_structured
        }'
      return
    fi
  fi

  # Fallback to cache
  check_cache
  local state
  state=$(get_state)

  local doc
  doc=$(echo "$state" | jq --arg id "$doc_id" '.documents[$id] // empty')
  if [ -z "$doc" ] || [ "$doc" = "null" ]; then
    echo "Document not found: $doc_id" >&2
    exit 1
  fi

  local panel_text
  panel_text=$(echo "$doc" | jq -r '.notes_markdown // empty')

  if [ -z "$panel_text" ]; then
    panel_text=$(echo "$state" | jq -r --arg id "$doc_id" '
      .documentPanels[$id] // {} |
      [to_entries[] | .value.content // empty] |
      [.. | select(.type? == "text") | .text] |
      join(" ")
    ' 2>/dev/null || echo "")
  fi

  local transcript_text
  transcript_text=$(echo "$state" | jq -r --arg id "$doc_id" '
    .transcripts[$id] // [] |
    [.[] | .text // empty] |
    join("\n")
  ' 2>/dev/null || echo "")

  local transcript_structured
  transcript_structured=$(echo "$state" | jq --arg id "$doc_id" '
    .transcripts[$id] // [] |
    [.[] | {
      text: (.text // ""),
      source: (.source // "unknown"),
      start: (.start_timestamp // null),
      end: (.end_timestamp // null)
    }]
  ' 2>/dev/null || echo "[]")

  echo "$doc" | jq \
    --arg panel "$panel_text" \
    --arg transcript "$transcript_text" \
    --argjson transcript_structured "$transcript_structured" \
    '{
      id: .id,
      title: .title,
      date: .created_at,
      attendees: [
        ((.people.creator // {}) | {name: (.details.person.name.fullName // .name // "unknown"), email: (.email // "")}),
        ((.people.attendees // [])[] | {name: (.details.person.name.fullName // .email), email: (.email // "")})
      ],
      panel_text: $panel,
      transcript_text: $transcript,
      transcript_structured: $transcript_structured
    }'
}

cmd_search() {
  local query="${1:-}"
  if [ -z "$query" ]; then
    echo "Usage: granola.sh search <query> [--folders \"Folder1,Folder2\"]" >&2
    exit 1
  fi
  shift

  local folders=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --folders)
        folders="$2"
        shift 2
        ;;
      *)
        echo "Unknown option: $1" >&2
        exit 1
        ;;
    esac
  done

  # If no folders specified, read configured folders from state file
  local state_file
  state_file="$(cd "$(dirname "$0")/.." && pwd)/.egregore-state.json"
  if [ -z "$folders" ] && [ -f "$state_file" ]; then
    folders=$(jq -r '.granola_folders // [] | join(",")' "$state_file")
  fi

  # If we have folders configured, list from each folder (API-aware) then filter
  if [ -n "$folders" ]; then
    local all_docs="[]"
    IFS=',' read -ra folder_arr <<< "$folders"
    for fname in "${folder_arr[@]}"; do
      fname=$(echo "$fname" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      local folder_docs
      folder_docs=$(cmd_list --folder "$fname" 2>/dev/null) || true
      if [ -n "$folder_docs" ] && [ "$folder_docs" != "[]" ]; then
        all_docs=$(echo "$all_docs" "$folder_docs" | jq -s '.[0] + .[1]')
      fi
    done
    echo "$all_docs" | jq --arg query "$query" '
      [.[] | select(
        (.title // "" | ascii_downcase | contains($query | ascii_downcase)) or
        ([(.attendees // [])[] | ascii_downcase] | any(contains($query | ascii_downcase)))
      )] | unique_by(.id) | sort_by(.date) | reverse
    '
  else
    # Search all documents
    local all_docs
    all_docs=$(cmd_list 2>/dev/null) || true
    if [ -n "$all_docs" ]; then
      echo "$all_docs" | jq --arg query "$query" '
        [.[] | select(
          (.title // "" | ascii_downcase | contains($query | ascii_downcase)) or
          ([(.attendees // [])[] | ascii_downcase] | any(contains($query | ascii_downcase)))
        )] | sort_by(.date) | reverse
      '
    else
      echo "[]"
    fi
  fi
}

# --- Main ---

case "${1:-help}" in
  test)
    cmd_test
    ;;
  folders)
    cmd_folders
    ;;
  list)
    shift
    cmd_list "$@"
    ;;
  get)
    shift
    cmd_get "$@"
    ;;
  search)
    shift
    cmd_search "$@"
    ;;
  help|*)
    echo "Usage: granola.sh <command>"
    echo ""
    echo "Commands:"
    echo "  test                                   Check API + cache status"
    echo "  folders                                List folders with doc counts"
    echo "  list [--folder X] [--since DATE] [--exclude id1,id2]"
    echo "                                         List meetings as JSON"
    echo "  get <doc-id>                           Full meeting data"
    echo "  search <query> [--folders X,Y]         Search by title/attendee across folders"
    echo ""
    echo "Data sources: API (live, preferred) → cache (offline fallback)"
    ;;
esac
