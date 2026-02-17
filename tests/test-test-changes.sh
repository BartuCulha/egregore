#!/bin/bash
# Tests for bin/test-changes.sh static analyzer
# Creates temp files with known antipatterns and verifies detection
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEST_SCRIPT="$SCRIPT_DIR/bin/test-changes.sh"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "=== test-changes.sh tests ==="

# --- Test 1: Detects unguarded date() call ---
cat > "$TMPDIR/bad-date.md" << 'EOF'
```cypher
MATCH (s:Session)
WHERE date(s.date) >= date() - duration('P7D')
RETURN s.topic
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/bad-date.md" 2>&1) || true
if echo "$OUTPUT" | grep -q 'FAIL.*Unguarded date()'; then
  pass "detects unguarded date() call"
else
  fail "missed unguarded date() call"
fi

# --- Test 2: Allows guarded date() with toString ---
cat > "$TMPDIR/good-date.md" << 'EOF'
```cypher
MATCH (s:Session)
WHERE date(left(toString(s.date), 10)) >= date() - duration('P7D')
RETURN s.topic
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/good-date.md" 2>&1)
if echo "$OUTPUT" | grep -q 'No unguarded date()'; then
  pass "allows guarded date() with toString"
else
  fail "false positive on guarded date() call"
fi

# --- Test 3: Detects .year/.month/.day on mixed-type field ---
cat > "$TMPDIR/bad-field.md" << 'EOF'
```cypher
WHERE a.created >= datetime({year: s.date.year, month: s.date.month, day: s.date.day})
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/bad-field.md" 2>&1) || true
if echo "$OUTPUT" | grep -q 'FAIL.*\.year/\.month/\.day'; then
  pass "detects .year/.month/.day on mixed-type field"
else
  fail "missed .year/.month/.day on mixed-type field"
fi

# --- Test 4: Allows .year/.month/.day on safe alias ---
cat > "$TMPDIR/good-field.md" << 'EOF'
```cypher
WITH s, date(left(toString(s.date), 10)) AS sDate
WHERE a.created >= datetime({year: sDate.year, month: sDate.month, day: sDate.day})
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/good-field.md" 2>&1)
if echo "$OUTPUT" | grep -q 'No .year/.month/.day'; then
  pass "allows .year/.month/.day on safe alias (sDate)"
else
  fail "false positive on safe alias .year/.month/.day"
fi

# --- Test 5: Detects macOS-incompatible date +%N ---
cat > "$TMPDIR/bad-bash.sh" << 'EOF'
#!/bin/bash
NANOS=$(date +%N)
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/bad-bash.sh" 2>&1) || true
if echo "$OUTPUT" | grep -q 'FAIL.*macOS-incompatible date'; then
  pass "detects macOS-incompatible date +%N"
else
  fail "missed macOS-incompatible date +%N"
fi

# --- Test 6: Detects direct curl to Neo4j ---
cat > "$TMPDIR/bad-curl.md" << 'EOF'
Run this:
```bash
curl -X POST http://localhost:7474/graph/query -d '{"query": "MATCH..."}'
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/bad-curl.md" 2>&1) || true
if echo "$OUTPUT" | grep -q 'FAIL.*Direct curl to Neo4j'; then
  pass "detects direct curl to Neo4j"
else
  fail "missed direct curl to Neo4j"
fi

# --- Test 7: JSON summary on last line ---
cat > "$TMPDIR/mixed.md" << 'EOF'
```cypher
MATCH (s:Session)
WHERE date(s.date) >= date()
RETURN s.topic
```
EOF
OUTPUT=$(bash "$TEST_SCRIPT" "$TMPDIR/mixed.md" 2>&1) || true
LAST_LINE=$(echo "$OUTPUT" | tail -1)
if echo "$LAST_LINE" | grep -qE '^\{"pass":[0-9]+,"fail":[0-9]+,"warn":[0-9]+\}$'; then
  pass "JSON summary on last line"
else
  fail "missing or malformed JSON summary: $LAST_LINE"
fi

# --- Test 8: Exit code is 1 when FAILs exist ---
EXIT_CODE=0
bash "$TEST_SCRIPT" "$TMPDIR/bad-date.md" >/dev/null 2>&1 || EXIT_CODE=$?
if [ "$EXIT_CODE" -eq 1 ]; then
  pass "exits 1 when FAILs exist"
else
  fail "should exit 1 with FAILs, got exit $EXIT_CODE"
fi

# --- Test 9: Exit code is 0 when only WARNs ---
cat > "$TMPDIR/warn-only.md" << 'EOF'
```cypher
MATCH (s:Session)
WHERE s.name = "test"
IN s.topics
RETURN s.topic
```
EOF
EXIT_CODE=0
bash "$TEST_SCRIPT" "$TMPDIR/warn-only.md" >/dev/null 2>&1 || EXIT_CODE=$?
if [ "$EXIT_CODE" -eq 0 ]; then
  pass "exits 0 when only WARNs"
else
  fail "should exit 0 with only WARNs, got exit $EXIT_CODE"
fi

# --- Test 10: No files = clean exit ---
EXIT_CODE=0
OUTPUT=$(bash "$TEST_SCRIPT" "/nonexistent/file.md" 2>&1) || EXIT_CODE=$?
if [ "$EXIT_CODE" -eq 0 ]; then
  pass "exits 0 with no testable files"
else
  fail "should exit 0 with no files, got exit $EXIT_CODE"
fi

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
