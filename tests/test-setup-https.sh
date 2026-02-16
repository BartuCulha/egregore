#!/bin/bash
# Tests for setup.md bug fix (#9)
# Verifies: all clone URLs use HTTPS, no SSH references remain
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETUP_MD="$SCRIPT_DIR/.claude/commands/setup.md"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== setup.md HTTPS tests ==="

# --- Test 1: no SSH clone URLs ---
SSH_CLONES=$(grep -c 'git clone git@github.com' "$SETUP_MD" || true)
if [ "$SSH_CLONES" -eq 0 ]; then
  pass "no SSH clone URLs (git@github.com)"
else
  fail "found $SSH_CLONES SSH clone URLs — should be HTTPS"
fi

# --- Test 2: HTTPS clone URLs present ---
HTTPS_CLONES=$(grep -c 'git clone https://github.com' "$SETUP_MD" || true)
if [ "$HTTPS_CLONES" -gt 0 ]; then
  pass "HTTPS clone URLs present ($HTTPS_CLONES found)"
else
  fail "no HTTPS clone URLs found"
fi

# --- Test 3: no SSH troubleshooting reference ---
SSH_DEBUG=$(grep -c 'ssh -T git@github.com' "$SETUP_MD" || true)
if [ "$SSH_DEBUG" -eq 0 ]; then
  pass "no SSH troubleshooting reference"
else
  fail "found SSH troubleshooting reference — should point to github-auth.sh"
fi

# --- Test 4: references github-auth.sh for auth issues ---
AUTH_REF=$(grep -c 'github-auth.sh' "$SETUP_MD" || true)
if [ "$AUTH_REF" -gt 0 ]; then
  pass "references github-auth.sh for authentication"
else
  fail "no reference to github-auth.sh — users won't know how to fix auth"
fi

# --- Test 5: header says HTTPS not SSH ---
if grep -q '## CRITICAL: Always use HTTPS' "$SETUP_MD"; then
  pass "header says 'Always use HTTPS'"
else
  fail "header still says SSH or is missing"
fi

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
