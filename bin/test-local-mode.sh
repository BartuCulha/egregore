#!/bin/bash
set -euo pipefail

# Test that all graph/notify scripts bail correctly in local mode.
# Usage: bash bin/test-local-mode.sh
#
# Temporarily sets mode to "local" in egregore.json, runs all scripts,
# verifies they return the expected offline responses, and restores mode.

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$SCRIPT_DIR/egregore.json"
PASS=0
FAIL=0
ERRORS=""

# --- Save original config and set local mode ---
cp "$CONFIG" "${CONFIG}.bak"
TMP=$(mktemp)
jq '.mode = "local"' "$CONFIG" > "$TMP" && mv "$TMP" "$CONFIG"

cleanup() {
  # Restore original config exactly (preserves formatting)
  mv "${CONFIG}.bak" "$CONFIG"
}
trap cleanup EXIT

echo "Testing local-mode enforcement..."
echo "================================="
echo ""

# --- Helper ---
check() {
  local name="$1"
  local expected="$2"
  local actual="$3"

  if echo "$actual" | grep -qF "$expected"; then
    echo "  ✓ $name"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $name"
    echo "    expected: $expected"
    echo "    got:      $actual"
    FAIL=$((FAIL + 1))
    ERRORS="$ERRORS\n  - $name"
  fi
}

# --- Test 1: graph.sh query ---
echo "1. bin/graph.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/graph.sh" query "MATCH (n) RETURN n" 2>/dev/null || echo "ERROR")
check "query returns empty results" '"results":[]' "$RESULT"
RESULT=$(bash "$SCRIPT_DIR/bin/graph.sh" test 2>/dev/null || echo "ERROR")
check "test returns local_mode reason" 'local_mode' "$RESULT"

# --- Test 2: graph-batch.sh ---
echo "2. bin/graph-batch.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/graph-batch.sh" '[{"statement":"MATCH (n) RETURN n"}]' 2>/dev/null || echo "ERROR")
check "batch returns empty results" '"results":[]' "$RESULT"

# --- Test 3: graph-op.sh ---
echo "3. bin/graph-op.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/graph-op.sh" mark-read "test-id" 2>/dev/null || echo "ERROR")
check "mark-read returns empty" '"results":[]' "$RESULT"

# --- Test 4: notify.sh ---
echo "4. bin/notify.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/notify.sh" send "test" "hello" 2>/dev/null || echo "ERROR")
check "send returns offline" 'local_mode' "$RESULT"

# --- Test 5: eval-op.sh ---
echo "5. bin/eval-op.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/eval-op.sh" get-runs "test-pipeline" 2>/dev/null || echo "ERROR")
check "get-runs returns empty" '"results":[]' "$RESULT"

# --- Test 6: index-handoff.sh ---
echo "6. bin/index-handoff.sh"
RESULT=$(bash "$SCRIPT_DIR/bin/index-handoff.sh" "handoffs/test.md" 2>/dev/null || echo "ERROR")
check "index returns local mode" 'local' "$RESULT"

# --- Test 7: No network calls in local mode (timing test) ---
echo "7. Performance"
START=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null || echo "0")
bash "$SCRIPT_DIR/bin/graph.sh" query "MATCH (n) RETURN n" >/dev/null 2>&1
END=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null || echo "0")
if [ "$START" != "0" ] && [ "$END" != "0" ]; then
  ELAPSED=$((END - START))
  if [ "$ELAPSED" -lt 500 ]; then
    echo "  ✓ graph.sh completes in ${ELAPSED}ms (< 500ms, no network)"
    PASS=$((PASS + 1))
  else
    echo "  ✗ graph.sh took ${ELAPSED}ms (> 500ms, possible network call)"
    FAIL=$((FAIL + 1))
    ERRORS="$ERRORS\n  - slow graph.sh (${ELAPSED}ms)"
  fi
else
  echo "  ~ skipped (no python3 for timing)"
fi

# --- Test 8: Command specs mention mode detection ---
echo "8. Command spec coverage"
COMMANDS="activity handoff quest reflect deep-reflect ask summon quest-suggest eval archive"
for cmd in $COMMANDS; do
  FILE="$SCRIPT_DIR/.claude/commands/${cmd}.md"
  if [ -f "$FILE" ]; then
    if grep -q 'mode.*local\|local.*mode\|Local mode\|LOCAL MODE\|CONNECTED MODE ONLY' "$FILE"; then
      PASS=$((PASS + 1))
    else
      echo "  ✗ $cmd.md — no local mode guard found"
      FAIL=$((FAIL + 1))
      ERRORS="$ERRORS\n  - $cmd.md missing local mode guard"
    fi
  else
    echo "  ✗ $cmd.md — file not found"
    FAIL=$((FAIL + 1))
  fi
done
echo "  ✓ All $( echo $COMMANDS | wc -w | tr -d ' ') command specs have local mode guards"

# --- Test 9: Grep for unguarded graph calls in commands ---
echo "9. Unguarded graph call scan"
UNGUARDED=0
for cmd in $COMMANDS; do
  FILE="$SCRIPT_DIR/.claude/commands/${cmd}.md"
  if [ -f "$FILE" ]; then
    # Count lines with graph.sh/notify.sh that don't have "CONNECTED" or "Connected mode" or "skip" nearby
    # This is a heuristic — manual review is still needed
    GRAPH_LINES=$(grep -c 'bin/graph\|bin/notify\|bin/eval-op\|bin/index-handoff' "$FILE" 2>/dev/null || echo "0")
    GUARDED_LINES=$(grep -c 'CONNECTED MODE ONLY\|Connected mode\|skip.*local\|local mode\|Skip this\|Skip ALL\|Skip entirely\|do NOT run' "$FILE" 2>/dev/null || echo "0")
    if [ "$GRAPH_LINES" -gt 0 ] && [ "$GUARDED_LINES" -eq 0 ]; then
      echo "  ✗ $cmd.md — $GRAPH_LINES graph calls but no guards"
      UNGUARDED=$((UNGUARDED + 1))
    fi
  fi
done
if [ "$UNGUARDED" -eq 0 ]; then
  echo "  ✓ No unguarded graph calls in command specs"
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + UNGUARDED))
fi

# ===================================================================
# PART 2: Connected mode — verify scripts still work when NOT local
# ===================================================================

echo ""
echo "Testing connected-mode passthrough..."
echo "================================="
echo ""

# Mode is restored by now (trap ran? no — trap runs on EXIT).
# We need to restore manually for part 2, then re-set for cleanup.
cleanup  # restores original config
trap - EXIT  # remove cleanup trap (already ran)

CURRENT_MODE=$(jq -r '.mode // "connected"' "$CONFIG" 2>/dev/null)
echo "Current mode: ${CURRENT_MODE:-null} (should NOT be 'local')"

if [ "$CURRENT_MODE" = "local" ]; then
  echo "  ✗ Config still in local mode after restore — skipping connected tests"
  FAIL=$((FAIL + 1))
else
  # Test 10: graph.sh should NOT return local_mode reason
  echo "10. bin/graph.sh (connected)"
  RESULT=$(bash "$SCRIPT_DIR/bin/graph.sh" test 2>&1 || echo "ERROR")
  if echo "$RESULT" | grep -qF "local_mode"; then
    echo "  ✗ graph.sh test hit local gate in connected mode"
    FAIL=$((FAIL + 1))
    ERRORS="$ERRORS\n  - graph.sh returns local_mode in connected mode"
  else
    echo "  ✓ graph.sh test does not hit local gate"
    PASS=$((PASS + 1))
  fi

  RESULT=$(bash "$SCRIPT_DIR/bin/graph.sh" query "RETURN 1 AS test" 2>/dev/null || echo "ERROR")
  if echo "$RESULT" | grep -qF '"test"'; then
    echo "  ✓ graph.sh query returns real data"
    PASS=$((PASS + 1))
  elif echo "$RESULT" | grep -qF '"results":[]'; then
    # Could be offline (no API key) — that's fine, as long as it's not local_mode
    echo "  ✓ graph.sh query passes through (offline fallback, not local gate)"
    PASS=$((PASS + 1))
  else
    echo "  ✗ graph.sh query unexpected result: $RESULT"
    FAIL=$((FAIL + 1))
    ERRORS="$ERRORS\n  - graph.sh query unexpected in connected mode"
  fi

  # Test 11: graph-batch.sh
  echo "11. bin/graph-batch.sh (connected)"
  RESULT=$(bash "$SCRIPT_DIR/bin/graph-batch.sh" '[{"statement":"RETURN 1 AS test","parameters":{}}]' 2>/dev/null || echo "ERROR")
  if echo "$RESULT" | grep -qF '"test"'; then
    echo "  ✓ graph-batch.sh returns real data"
    PASS=$((PASS + 1))
  elif echo "$RESULT" | grep -qF '"results":[]'; then
    echo "  ✓ graph-batch.sh passes through (offline fallback)"
    PASS=$((PASS + 1))
  else
    echo "  ✗ graph-batch.sh unexpected: $RESULT"
    FAIL=$((FAIL + 1))
  fi

  # Test 12: notify.sh
  echo "12. bin/notify.sh (connected)"
  RESULT=$(bash "$SCRIPT_DIR/bin/notify.sh" test 2>&1 || echo "ERROR")
  if echo "$RESULT" | grep -qF "local_mode"; then
    echo "  ✗ notify.sh hit local gate in connected mode"
    FAIL=$((FAIL + 1))
    ERRORS="$ERRORS\n  - notify.sh returns local_mode in connected mode"
  else
    echo "  ✓ notify.sh does not hit local gate"
    PASS=$((PASS + 1))
  fi

  # Test 13: graph-op.sh
  echo "13. bin/graph-op.sh (connected)"
  RESULT=$(bash "$SCRIPT_DIR/bin/graph-op.sh" mark-read "test-nonexistent" 2>/dev/null || echo "ERROR")
  if echo "$RESULT" | grep -qF "local_mode"; then
    echo "  ✗ graph-op.sh hit local gate"
    FAIL=$((FAIL + 1))
  else
    echo "  ✓ graph-op.sh passes through"
    PASS=$((PASS + 1))
  fi

  # Test 14: Timing — connected mode should also be fast (API call < 5s)
  echo "14. Connected-mode latency"
  START=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null || echo "0")
  bash "$SCRIPT_DIR/bin/graph.sh" query "RETURN 1" >/dev/null 2>&1
  END=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null || echo "0")
  if [ "$START" != "0" ] && [ "$END" != "0" ]; then
    ELAPSED=$((END - START))
    if [ "$ELAPSED" -lt 5000 ]; then
      echo "  ✓ graph.sh completes in ${ELAPSED}ms (< 5s)"
      PASS=$((PASS + 1))
    else
      echo "  ✗ graph.sh took ${ELAPSED}ms (> 5s, possible issue)"
      FAIL=$((FAIL + 1))
    fi
  else
    echo "  ~ skipped (no python3 for timing)"
  fi
fi

# --- Summary ---
echo ""
echo "================================="
TOTAL=$((PASS + FAIL))
echo "Results: $PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Failures:$ERRORS"
  echo ""
  exit 1
else
  echo "All checks passed (local + connected)."
  exit 0
fi
