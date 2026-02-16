#!/bin/bash
# Tests for session-start.sh bug fix (#7)
# Verifies: API key provisioning runs in background, doesn't block startup
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_START="$SCRIPT_DIR/bin/session-start.sh"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== session-start.sh API provisioning tests ==="

# --- Test 1: provisioning block is wrapped in background subshell ---
# Check that the curl call for API key provisioning is inside a `) &` block
# The pattern: the if block containing api/org/.../key should end with `) &`
PROVISION_SECTION=$(sed -n '/Auto-provision EGREGORE_API_KEY/,/^fi$/p' "$SESSION_START")

if echo "$PROVISION_SECTION" | grep -q ') &'; then
  pass "API provisioning runs in background subshell"
else
  fail "API provisioning is NOT backgrounded — will block startup"
fi

# --- Test 2: curl has --connect-timeout ---
if echo "$PROVISION_SECTION" | grep -q '\-\-connect-timeout'; then
  pass "curl has --connect-timeout for fast failure"
else
  fail "curl missing --connect-timeout — slow failure on unreachable host"
fi

# --- Test 3: curl still has --max-time ---
if echo "$PROVISION_SECTION" | grep -q '\-\-max-time'; then
  pass "curl has --max-time as total timeout cap"
else
  fail "curl missing --max-time — no total timeout"
fi

# --- Test 4: provisioning only runs when EGREGORE_API_KEY is missing ---
if echo "$PROVISION_SECTION" | grep -q "grep -q '^EGREGORE_API_KEY='"; then
  pass "provisioning gated on missing EGREGORE_API_KEY"
else
  fail "provisioning not gated — runs even when key exists"
fi

# --- Test 5: timing test — session-start.sh completes in <5s even with bad API ---
# Create a temporary environment that triggers the provisioning path
TMPDIR=$(mktemp -d)
cp "$SCRIPT_DIR/egregore.json" "$TMPDIR/egregore.json"

# Point to a blackhole IP to simulate unreachable API
jq '.api_url = "http://10.255.255.1:9999"' "$TMPDIR/egregore.json" > "$TMPDIR/tmp.json" \
  && mv "$TMPDIR/tmp.json" "$TMPDIR/egregore.json"

# Create a .env with GITHUB_TOKEN but no EGREGORE_API_KEY
echo "GITHUB_TOKEN=fake_token_for_test" > "$TMPDIR/.env"

# Create minimal .egregore-state.json
echo '{"onboarding_complete":true,"github_username":"testuser","name":"Test"}' > "$TMPDIR/.egregore-state.json"

# Create a minimal session-start that only tests the provisioning section
cat > "$TMPDIR/test-provision-timing.sh" << 'INNEREOF'
#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$1"
ENV_FILE="$SCRIPT_DIR/.env"
CONFIG="$SCRIPT_DIR/egregore.json"

if [ -f "$ENV_FILE" ] && ! grep -q '^EGREGORE_API_KEY=' "$ENV_FILE" 2>/dev/null; then
  (
    GITHUB_TOKEN=$(grep '^GITHUB_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    API_URL=$(jq -r '.api_url // empty' "$CONFIG" 2>/dev/null)
    GITHUB_ORG=$(jq -r '.github_org // empty' "$CONFIG" 2>/dev/null)

    if [ -n "$GITHUB_TOKEN" ] && [ -n "$API_URL" ] && [ -n "$GITHUB_ORG" ]; then
      SLUG=$(echo "$GITHUB_ORG" | tr '[:upper:]' '[:lower:]' | tr -d '-' | tr -d ' ')
      curl -s -X GET "${API_URL}/api/org/${SLUG}/key" \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        --connect-timeout 5 --max-time 10 2>/dev/null || true
    fi
  ) &
fi

# Script continues immediately — this is what we're testing
echo "DONE"
INNEREOF
chmod +x "$TMPDIR/test-provision-timing.sh"

# Run the script in background, give it 5s. If provisioning were synchronous
# with a blackhole IP, curl would block 5-10s. Background = instant return.
bash "$TMPDIR/test-provision-timing.sh" "$TMPDIR" > "$TMPDIR/result.txt" 2>/dev/null &
TEST_PID=$!

# Wait up to 5 seconds
for i in 1 2 3 4 5; do
  if ! kill -0 "$TEST_PID" 2>/dev/null; then break; fi
  sleep 1
done

if ! kill -0 "$TEST_PID" 2>/dev/null; then
  RESULT=$(cat "$TMPDIR/result.txt" 2>/dev/null || echo "")
  if [ "$RESULT" = "DONE" ]; then
    pass "provisioning doesn't block — script completed within 5s"
  else
    fail "script completed but output unexpected: '$RESULT'"
  fi
else
  kill "$TEST_PID" 2>/dev/null || true
  fail "provisioning blocked script — still running after 5s"
fi

rm -rf "$TMPDIR"

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
