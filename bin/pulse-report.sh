#!/bin/bash
set -euo pipefail

# Pulse Weekly Report — aggregates .pulse/runs.jsonl and sends via Telegram.
# Designed to run on a weekly cron. Pure jq aggregation, no LLM.
#
# Usage: bash bin/pulse-report.sh [days=7] [recipient=cemfd]

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NOTIFY="$SCRIPT_DIR/bin/notify.sh"
PULSE_LOG="$SCRIPT_DIR/.pulse/runs.jsonl"
DAYS="${1:-7}"
RECIPIENT="${2:-cemfd}"

if [ ! -f "$PULSE_LOG" ] || [ ! -s "$PULSE_LOG" ]; then
  echo "No pulse data yet."
  exit 0
fi

# Filter to last N days
CUTOFF=$(python3 -c "from datetime import datetime, timedelta; print((datetime.utcnow() - timedelta(days=$DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ'))" 2>/dev/null)

RECENT=$(jq -c "select(.timestamp >= \"$CUTOFF\")" "$PULSE_LOG" 2>/dev/null)
TOTAL=$(echo "$RECENT" | grep -c '^' 2>/dev/null || echo "0")

if [ "$TOTAL" -eq 0 ]; then
  bash "$NOTIFY" send "$RECIPIENT" "Pulse report (${DAYS}d): No runs recorded." 2>/dev/null
  exit 0
fi

# --- Aggregate ---
STATS=$(echo "$RECENT" | jq -s '{
  total_runs: length,
  edges: {
    continues: [.[].response.edges[]? | select(.type == "CONTINUES")] | length,
    involves: [.[].response.edges[]? | select(.type == "INVOLVES")] | length,
    total: [.[].response.edges[]?] | length
  },
  signals: {
    convergence: [.[].response.signals[]? | select(.type == "convergence")] | length,
    tension: [.[].response.signals[]? | select(.type == "tension")] | length,
    synthesis_deficit: [.[].response.signals[]? | select(.type == "synthesis_deficit")] | length,
    total: [.[].response.signals[]?] | length
  },
  top_quests: (
    [.[].response.edges[]? | select(.type == "INVOLVES") | .target_id] |
    group_by(.) | map({quest: .[0], count: length}) | sort_by(-.count) | .[0:5]
  ),
  top_recommendations: (
    [.[].response.recommendations[]?] | unique | .[0:5]
  ),
  avg_edges_per_run: (
    ([.[].response.edges | length] | add // 0) / (length | if . == 0 then 1 else . end) | . * 10 | round / 10
  ),
  sessions: [.[].session_id] | unique | length
}' 2>/dev/null)

# --- Format message ---
RUNS=$(echo "$STATS" | jq -r '.total_runs')
SESSIONS=$(echo "$STATS" | jq -r '.sessions')
E_TOTAL=$(echo "$STATS" | jq -r '.edges.total')
E_CONT=$(echo "$STATS" | jq -r '.edges.continues')
E_INV=$(echo "$STATS" | jq -r '.edges.involves')
S_TOTAL=$(echo "$STATS" | jq -r '.signals.total')
S_CONV=$(echo "$STATS" | jq -r '.signals.convergence')
S_TENS=$(echo "$STATS" | jq -r '.signals.tension')
S_SYNTH=$(echo "$STATS" | jq -r '.signals.synthesis_deficit')
AVG_E=$(echo "$STATS" | jq -r '.avg_edges_per_run')

QUEST_LIST=$(echo "$STATS" | jq -r '.top_quests[]? | "  \(.quest) (\(.count)x)"' 2>/dev/null)
REC_LIST=$(echo "$STATS" | jq -r '.top_recommendations[]? | "  - \(.)"' 2>/dev/null | head -5)

MSG="Pulse report (${DAYS}d)

Sessions: ${SESSIONS} | Runs: ${RUNS}
Edges: ${E_TOTAL} (${E_CONT} continues, ${E_INV} involves)
Avg edges/run: ${AVG_E}
Signals: ${S_TOTAL} (${S_CONV} conv, ${S_TENS} tension, ${S_SYNTH} synth deficit)"

if [ -n "$QUEST_LIST" ]; then
  MSG="${MSG}

Top quests:
${QUEST_LIST}"
fi

if [ -n "$REC_LIST" ]; then
  MSG="${MSG}

Recommendations:
${REC_LIST}"
fi

# --- Send ---
bash "$NOTIFY" send "$RECIPIENT" "$MSG" 2>/dev/null
echo "$MSG"

# --- Also save report locally ---
REPORT_DIR="$SCRIPT_DIR/.pulse/reports"
mkdir -p "$REPORT_DIR" 2>/dev/null
REPORT_FILE="$REPORT_DIR/$(date -u +%Y-%m-%d).json"
echo "$STATS" | jq --arg period "${DAYS}d" --arg generated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '. + {period: $period, generated: $generated}' > "$REPORT_FILE" 2>/dev/null

echo "Report saved to $REPORT_FILE"
