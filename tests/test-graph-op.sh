#!/bin/bash
# Tests for graph-op.sh bug fixes (#2 + #3)
# Verifies: set -euo pipefail present, errors propagate, missing args caught
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
GRAPH_OP="$SCRIPT_DIR/bin/graph-op.sh"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== graph-op.sh tests ==="

# --- Test 1: set -euo pipefail is present ---
if head -5 "$GRAPH_OP" | grep -q 'set -euo pipefail'; then
  pass "has set -euo pipefail"
else
  fail "missing set -euo pipefail"
fi

# --- Test 2: no 2>/dev/null on graph.sh calls ---
# Count graph.sh query calls that suppress stderr
SUPPRESSED=$(grep 'graph\.sh.*query' "$GRAPH_OP" | grep -c '2>/dev/null' || true)
if [ "$SUPPRESSED" -eq 0 ]; then
  pass "no stderr suppression on graph.sh calls"
else
  fail "found $SUPPRESSED graph.sh calls with 2>/dev/null"
fi

# --- Test 3: no-args exits non-zero ---
OUTPUT=$(bash "$GRAPH_OP" 2>&1 || true)
EXIT_CODE=0
bash "$GRAPH_OP" >/dev/null 2>&1 || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "no-args exits non-zero (exit $EXIT_CODE)"
else
  fail "no-args exits 0 — should be non-zero"
fi

# --- Test 4: unknown operation exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" "fake-op" >/dev/null 2>&1 || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "unknown operation exits non-zero (exit $EXIT_CODE)"
else
  fail "unknown operation exits 0 — should be non-zero"
fi

# --- Test 5: mark-read with no session-id exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" mark-read 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "mark-read with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "mark-read with no args exits 0 — should be non-zero"
fi

# --- Test 6: mark-done with no session-id exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" mark-done 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "mark-done with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "mark-done with no args exits 0 — should be non-zero"
fi

# --- Test 7: answer-question with no id exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" answer-question 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "answer-question with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "answer-question with no args exits 0 — should be non-zero"
fi

# --- Test 8: resolve-handoffs with no user exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" resolve-handoffs 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "resolve-handoffs with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "resolve-handoffs with no args exits 0 — should be non-zero"
fi

# --- Test 9: set-topic with no session-id exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" set-topic 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "set-topic with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "set-topic with no args exits 0 — should be non-zero"
fi

# --- Test 10: set-topic with session-id but no topic exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" set-topic "test-sid" 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "set-topic with no topic exits non-zero (exit $EXIT_CODE)"
else
  fail "set-topic with no topic exits 0 — should be non-zero"
fi

# --- Test 11: set-topic appears in operations list ---
OUTPUT=$(bash "$GRAPH_OP" "fake-op" 2>&1 || true)
if echo "$OUTPUT" | grep -q 'set-topic'; then
  pass "set-topic listed in operations"
else
  fail "set-topic NOT listed in operations error message"
fi

# --- Test 12: record-focus with missing args exits non-zero ---
EXIT_CODE=0
bash "$GRAPH_OP" record-focus 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "record-focus with no args exits non-zero (exit $EXIT_CODE)"
else
  fail "record-focus with no args exits 0 — should be non-zero"
fi

# --- Test 13: graph.sh failure propagates ---
# Create a fake graph.sh that always fails
TMPDIR=$(mktemp -d)
cat > "$TMPDIR/graph.sh" << 'EOF'
#!/bin/bash
echo "API error: connection refused" >&2
exit 1
EOF
chmod +x "$TMPDIR/graph.sh"

# Temporarily patch GS path by creating a wrapper
cat > "$TMPDIR/graph-op-test.sh" << WRAPPER
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$SCRIPT_DIR"
GS="$TMPDIR/graph.sh"
OP="\${1:-}"
shift || true
$(sed -n '/^case/,/^esac/p' "$GRAPH_OP")
WRAPPER
chmod +x "$TMPDIR/graph-op-test.sh"

EXIT_CODE=0
bash "$TMPDIR/graph-op-test.sh" mark-read "test-id" 2>/dev/null || EXIT_CODE=$?
if [ "$EXIT_CODE" -ne 0 ]; then
  pass "graph.sh failure propagates to caller (exit $EXIT_CODE)"
else
  fail "graph.sh failure did NOT propagate — caller got exit 0"
fi

rm -rf "$TMPDIR"

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
