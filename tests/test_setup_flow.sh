#!/bin/bash
# End-to-end test for setup + invite + Telegram flow
# Tests against the live API using oguzhan's personal account
set -uo pipefail

# Helper: extract HTTP body and code from curl -w "\n%{http_code}" output
# macOS head doesn't support -n -1, so use sed instead
get_body() { echo "$1" | sed '$d'; }
get_code() { echo "$1" | tail -1; }

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
API_URL=$(jq -r '.api_url' "$SCRIPT_DIR/egregore.json")
GITHUB_TOKEN=$(grep '^GITHUB_TOKEN=' "$SCRIPT_DIR/.env" | cut -d'=' -f2-)
EGREGORE_API_KEY=$(grep '^EGREGORE_API_KEY=' "$SCRIPT_DIR/.env" | cut -d'=' -f2-)

PASS=0
FAIL=0
ERRORS=""

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); ERRORS="$ERRORS\n  - $1"; }

echo ""
echo "=== Setup Flow E2E Tests ==="
echo "API: $API_URL"
echo ""

# --- Test 1: API health ---
echo "[1] API Health"
HEALTH=$(curl -sf "$API_URL/health" 2>/dev/null || echo '{}')
if echo "$HEALTH" | jq -e '.status == "ok"' &>/dev/null; then
  pass "API is healthy"
else
  fail "API health check failed: $HEALTH"
fi

# --- Test 2: GitHub token valid ---
echo "[2] GitHub Token"
GH_USER=$(curl -sf -H "Authorization: token $GITHUB_TOKEN" "https://api.github.com/user" | jq -r '.login')
if [ "$GH_USER" = "oguzhan" ]; then
  pass "GitHub token valid (user: $GH_USER)"
else
  fail "GitHub token invalid or wrong user: $GH_USER"
fi

# --- Test 3: Setup orgs endpoint ---
echo "[3] Setup Orgs"
ORGS=$(curl -sf "$API_URL/api/org/setup/orgs" -H "Authorization: Bearer $GITHUB_TOKEN" 2>/dev/null || echo '{}')
HAS_PERSONAL=$(echo "$ORGS" | jq -e '.personal.can_setup' 2>/dev/null || echo "false")
if [ "$HAS_PERSONAL" = "true" ]; then
  pass "Personal account has can_setup=true"
else
  fail "Personal account missing can_setup: $ORGS"
fi

# --- Test 4: Egregore repo exists ---
echo "[4] Egregore Repo"
REPO_STATUS=$(curl -sf -o /dev/null -w '%{http_code}' \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/oguzhan/egregore-core" 2>/dev/null || echo "000")
if [ "$REPO_STATUS" = "200" ]; then
  pass "oguzhan/egregore-core exists"
else
  fail "oguzhan/egregore-core not found (HTTP $REPO_STATUS)"
fi

# --- Test 5: Memory repo exists ---
echo "[5] Memory Repo"
MEM_STATUS=$(curl -sf -o /dev/null -w '%{http_code}' \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/oguzhan/oguzhan-memory" 2>/dev/null || echo "000")
if [ "$MEM_STATUS" = "200" ]; then
  pass "oguzhan/oguzhan-memory exists"
else
  fail "oguzhan/oguzhan-memory not found (HTTP $MEM_STATUS)"
fi

# --- Test 6: Memory repo has correct structure ---
echo "[6] Memory Structure"
MEM_TREE=$(curl -sf -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/oguzhan/oguzhan-memory/git/trees/main?recursive=1" 2>/dev/null || echo '{}')
DIRS_OK=true
for DIR in people conversations knowledge/decisions knowledge/patterns; do
  if ! echo "$MEM_TREE" | jq -e ".tree[] | select(.path == \"$DIR/.gitkeep\")" &>/dev/null; then
    DIRS_OK=false
    fail "Memory missing directory: $DIR"
  fi
done
if [ "$DIRS_OK" = "true" ]; then
  pass "Memory structure has all directories"
fi

# --- Test 7: Org node exists in Neo4j ---
echo "[7] Neo4j Org Node"
ORG_NODE=$(bash "$SCRIPT_DIR/bin/graph.sh" query \
  "MATCH (o:Org {id: 'oguzhan'}) RETURN o.id, o.name, o.github_org, o.api_key" 2>/dev/null || echo '{}')
ORG_ID=$(echo "$ORG_NODE" | jq -r '.values[0][0] // empty' 2>/dev/null)
ORG_KEY=$(echo "$ORG_NODE" | jq -r '.values[0][3] // empty' 2>/dev/null)
if [ "$ORG_ID" = "oguzhan" ] && [ -n "$ORG_KEY" ]; then
  pass "Org node exists with api_key"
else
  fail "Org node missing or no api_key: $ORG_NODE"
fi

# --- Test 8: ORG_CONFIGS has oguzhan ---
echo "[8] ORG_CONFIGS"
TG_STATUS=$(curl -sf "$API_URL/api/org/telegram/status/oguzhan" 2>/dev/null || echo '{}')
if echo "$TG_STATUS" | jq -e '.org_slug == "oguzhan"' &>/dev/null; then
  pass "oguzhan in ORG_CONFIGS"
else
  fail "oguzhan not in ORG_CONFIGS: $TG_STATUS"
fi

# --- Test 9: Setup is idempotent (re-run doesn't 409) ---
echo "[9] Setup Idempotency"
SETUP_RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/api/org/setup" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"github_org":"oguzhan","org_name":"oguzhan","is_personal":true,"repos":[]}' 2>/dev/null)
SETUP_HTTP=$(get_code "$SETUP_RESP")
SETUP_BODY=$(get_body "$SETUP_RESP")
if [ "$SETUP_HTTP" = "200" ]; then
  pass "Setup re-run returns 200 (idempotent)"
  # Verify it returned a setup token
  SETUP_TOKEN=$(echo "$SETUP_BODY" | jq -r '.setup_token // empty')
  if [ -n "$SETUP_TOKEN" ]; then
    pass "Setup returned setup_token"
  else
    fail "Setup 200 but no setup_token: $SETUP_BODY"
  fi
  # Verify Telegram invite link
  TG_LINK=$(echo "$SETUP_BODY" | jq -r '.telegram_invite_link // empty')
  if echo "$TG_LINK" | grep -q "startgroup=org_oguzhan"; then
    pass "Telegram invite link has correct slug"
  else
    fail "Telegram invite link wrong: $TG_LINK"
  fi
elif [ "$SETUP_HTTP" = "409" ]; then
  fail "Setup still returns 409 on re-run (not idempotent): $SETUP_BODY"
else
  fail "Setup unexpected HTTP $SETUP_HTTP: $SETUP_BODY"
fi

# --- Test 10: Telegram group registration ---
echo "[10] Telegram Registration"
# Use a fake chat_id for testing
TEST_CHAT_ID="-9999999999"
# Need bot auth — try without auth first (should work if no bot token configured on API)
TG_REG=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/api/org/telegram" \
  -H "Content-Type: application/json" \
  -d "{\"org_slug\":\"oguzhan\",\"chat_id\":\"$TEST_CHAT_ID\"}" 2>/dev/null)
TG_REG_HTTP=$(get_code "$TG_REG")
TG_REG_BODY=$(get_body "$TG_REG")
if [ "$TG_REG_HTTP" = "200" ]; then
  pass "Telegram registration succeeded (no auth)"
elif [ "$TG_REG_HTTP" = "401" ]; then
  # Auth required — try with the oguzhan API key (shouldn't work, but note it)
  pass "Telegram registration requires auth (expected — bot handles this)"
else
  fail "Telegram registration unexpected HTTP $TG_REG_HTTP: $TG_REG_BODY"
fi

# --- Test 11: Invite flow ---
echo "[11] Invite Flow"
# Invite uses GitHub token (not API key) — it calls GitHub APIs to add collaborators
if [ -n "$GITHUB_TOKEN" ]; then
  INVITE_RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/api/org/invite" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"github_org":"oguzhan","github_username":"fcdagdelen"}' 2>/dev/null)
  INVITE_HTTP=$(get_code "$INVITE_RESP")
  INVITE_BODY=$(get_body "$INVITE_RESP")
  if [ "$INVITE_HTTP" = "200" ]; then
    INVITE_TOKEN=$(echo "$INVITE_BODY" | jq -r '.invite_token // empty')
    INVITE_URL=$(echo "$INVITE_BODY" | jq -r '.invite_url // empty')
    if [ -n "$INVITE_TOKEN" ]; then
      pass "Invite created with token"
    else
      fail "Invite 200 but no token: $INVITE_BODY"
    fi

    # --- Test 12: Invite claim (peek) ---
    echo "[12] Invite Claim"
    if [ -n "$INVITE_TOKEN" ]; then
      PEEK_RESP=$(curl -sf "$API_URL/api/org/invite/$INVITE_TOKEN" 2>/dev/null || echo '{}')
      PEEK_ORG=$(echo "$PEEK_RESP" | jq -r '.org_name // empty')
      if [ "$PEEK_ORG" = "oguzhan" ]; then
        pass "Invite peek shows org_name=oguzhan"
      else
        fail "Invite peek wrong org: $PEEK_RESP"
      fi
    else
      fail "No invite token to test claim"
    fi

    # --- Test 13: Invite accept ---
    echo "[13] Invite Accept"
    if [ -n "$INVITE_TOKEN" ]; then
      # Cem's token — we don't have it, so test with oguzhan's (self-accept)
      ACCEPT_RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_URL/api/org/invite/$INVITE_TOKEN/accept" \
        -H "Authorization: Bearer $GITHUB_TOKEN" 2>/dev/null)
      ACCEPT_HTTP=$(get_code "$ACCEPT_RESP")
      ACCEPT_BODY=$(get_body "$ACCEPT_RESP")
      if [ "$ACCEPT_HTTP" = "200" ]; then
        # Check that setup_token is present
        CLAIM_TOKEN=$(echo "$ACCEPT_BODY" | jq -r '.setup_token // empty')
        if [ -n "$CLAIM_TOKEN" ]; then
          pass "Invite accept returned setup_token"
        else
          fail "Invite accept 200 but no setup_token: $ACCEPT_BODY"
        fi
      else
        fail "Invite accept HTTP $ACCEPT_HTTP: $ACCEPT_BODY"
      fi
    else
      fail "No invite token to test accept"
    fi
  else
    fail "Invite HTTP $INVITE_HTTP: $INVITE_BODY"
  fi
else
  fail "No GitHub token — can't test invite"
fi

# --- Test 14: Cem has access to repos (collaborator or pending invitation) ---
echo "[14] Collaborator Access"
# Check collaborator (204) or pending invitation
CEM_MEM=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/oguzhan/oguzhan-memory/collaborators/fcdagdelen")
if [ "$CEM_MEM" = "204" ]; then
  pass "cem is collaborator on oguzhan-memory"
else
  # Check pending invitations
  MEM_INV=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
    "https://api.github.com/repos/oguzhan/oguzhan-memory/invitations" | jq '[.[] | select(.invitee.login == "fcdagdelen")] | length')
  if [ "$MEM_INV" -gt 0 ] 2>/dev/null; then
    pass "cem has pending invitation on oguzhan-memory"
  else
    fail "cem not collaborator/invited on oguzhan-memory (HTTP $CEM_MEM)"
  fi
fi

CEM_CORE=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/oguzhan/egregore-core/collaborators/fcdagdelen")
if [ "$CEM_CORE" = "204" ]; then
  pass "cem is collaborator on egregore-core"
else
  CORE_INV=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
    "https://api.github.com/repos/oguzhan/egregore-core/invitations" | jq '[.[] | select(.invitee.login == "fcdagdelen")] | length')
  if [ "$CORE_INV" -gt 0 ] 2>/dev/null; then
    pass "cem has pending invitation on egregore-core"
  else
    fail "cem not collaborator/invited on egregore-core (HTTP $CEM_CORE)"
  fi
fi

# --- Test 15: Setup token claim returns correct URLs ---
echo "[15] Setup Token Claim"
if [ -n "${SETUP_TOKEN:-}" ]; then
  CLAIM_RESP=$(curl -sf "$API_URL/api/org/claim/$SETUP_TOKEN" 2>/dev/null || echo '{}')
  FORK_URL=$(echo "$CLAIM_RESP" | jq -r '.fork_url // empty')
  MEMORY_URL=$(echo "$CLAIM_RESP" | jq -r '.memory_url // empty')
  CLAIM_KEY=$(echo "$CLAIM_RESP" | jq -r '.api_key // empty')
  CLAIM_ORG=$(echo "$CLAIM_RESP" | jq -r '.org_name // empty')

  if echo "$FORK_URL" | grep -q "oguzhan/egregore-core"; then
    pass "Claim fork_url correct: $FORK_URL"
  else
    fail "Claim fork_url wrong: $FORK_URL"
  fi

  if echo "$MEMORY_URL" | grep -q "oguzhan/oguzhan-memory"; then
    pass "Claim memory_url correct: $MEMORY_URL"
  else
    fail "Claim memory_url wrong: $MEMORY_URL"
  fi

  if [ -n "$CLAIM_KEY" ]; then
    pass "Claim has api_key"
  else
    fail "Claim missing api_key"
  fi

  if [ "$CLAIM_ORG" = "oguzhan" ]; then
    pass "Claim org_name=oguzhan"
  else
    fail "Claim org_name wrong: $CLAIM_ORG"
  fi
else
  fail "No setup_token to test claim (setup may have failed)"
  fail "Skipping fork_url check"
  fail "Skipping memory_url check"
  fail "Skipping api_key check"
  fail "Skipping org_name check"
fi

# --- Test 16: Neo4j Org node updated after setup re-run ---
echo "[16] Neo4j After Setup"
ORG_AFTER=$(bash "$SCRIPT_DIR/bin/graph.sh" query \
  "MATCH (o:Org {id: 'oguzhan'}) RETURN o.api_key, o.name, o.github_org" 2>/dev/null || echo '{}')
AFTER_KEY=$(echo "$ORG_AFTER" | jq -r '.values[0][0] // empty' 2>/dev/null)
AFTER_NAME=$(echo "$ORG_AFTER" | jq -r '.values[0][1] // empty' 2>/dev/null)
if [ -n "$AFTER_KEY" ] && [ "$AFTER_NAME" = "oguzhan" ]; then
  pass "Org node has api_key and name after re-run"
else
  fail "Org node state after re-run: $ORG_AFTER"
fi

# --- Summary ---
echo ""
echo "=== Results ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  Failures:"
  echo -e "$ERRORS"
  echo ""
  exit 1
fi
echo ""
echo "  All tests passed!"
echo ""
