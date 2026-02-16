#!/bin/bash
# Integration test for multi-repo support
# Uses real git repos (bare repos as remotes) — no mocks, no GitHub
set -uo pipefail

# Resolve real repo path BEFORE any cd
REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

PASS=0
FAIL=0
ERRORS=""

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); ERRORS="$ERRORS\n  - $1"; }

# --- Setup: create a full Egregore workspace in /tmp ---
WORKSPACE=$(mktemp -d)
HUB="$WORKSPACE/egregore-core"
MANAGED_REPO="$WORKSPACE/myapp"
MEMORY="$WORKSPACE/test-memory"
REMOTE_HUB="$WORKSPACE/remotes/egregore-core.git"
REMOTE_MANAGED="$WORKSPACE/remotes/myapp.git"
REMOTE_MEMORY="$WORKSPACE/remotes/test-memory.git"

cleanup() { rm -rf "$WORKSPACE"; }
trap cleanup EXIT

echo ""
echo "=== Multi-Repo Integration Tests ==="
echo "Workspace: $WORKSPACE"
echo ""

# --- Create bare remotes ---
echo "[setup] Creating bare remotes..."
mkdir -p "$WORKSPACE/remotes"
git init --bare "$REMOTE_HUB" --quiet
git init --bare "$REMOTE_MANAGED" --quiet
git init --bare "$REMOTE_MEMORY" --quiet

# --- Initialize hub repo ---
echo "[setup] Initializing hub..."
mkdir -p "$HUB/bin" "$HUB/.claude/commands"
cd "$HUB"
git init --quiet
git remote add origin "$REMOTE_HUB"

# Create egregore.json with managed repo
cat > "$HUB/egregore.json" << 'EJSON'
{
  "org_name": "TestOrg",
  "github_org": "test-org",
  "memory_repo": "https://example.com/test-memory.git",
  "api_url": "http://localhost:9999",
  "slug": "testorg",
  "repos": ["myapp"]
}
EJSON

# Create minimal .env (graph/api calls will fail silently — that's fine)
cat > "$HUB/.env" << 'ENV'
GITHUB_TOKEN=fake-token-for-testing
EGREGORE_API_KEY=ek_fake
ENV

# Create .egregore-state.json
cat > "$HUB/.egregore-state.json" << 'STATE'
{
  "github_username": "testuser",
  "github_name": "Test User",
  "name": "Test User",
  "onboarding_complete": true,
  "usage_type": "founder_group"
}
STATE

# Copy session-start.sh from actual repo
cp "$REAL_REPO/bin/session-start.sh" "$HUB/bin/session-start.sh"
# Create stub graph.sh (tests don't need real Neo4j)
cat > "$HUB/bin/graph.sh" << 'GRAPHSTUB'
#!/bin/bash
echo '{"values":[]}'
GRAPHSTUB
chmod +x "$HUB/bin/graph.sh"

# Initial commit + push to remote
git add -A
git commit -m "Initial commit" --quiet
git push origin main --quiet 2>/dev/null || git push -u origin HEAD:main --quiet

# Create develop branch
git checkout -b develop --quiet
git push -u origin develop --quiet

# --- Initialize managed repo (myapp) ---
echo "[setup] Initializing managed repo (myapp)..."
mkdir -p "$MANAGED_REPO/src"
cd "$MANAGED_REPO"
git init --quiet
git remote add origin "$REMOTE_MANAGED"
echo "# MyApp" > README.md
echo "console.log('hello')" > src/index.js
git add -A
git commit -m "Initial commit" --quiet
git push -u origin main --quiet 2>/dev/null || git push -u origin HEAD:main --quiet

# Create develop branch in managed repo
git checkout -b develop --quiet
git push -u origin develop --quiet

# --- Initialize memory repo ---
echo "[setup] Initializing memory..."
mkdir -p "$MEMORY/people" "$MEMORY/handoffs" "$MEMORY/knowledge/decisions"
cd "$MEMORY"
git init --quiet
git remote add origin "$REMOTE_MEMORY"
touch people/.gitkeep handoffs/.gitkeep knowledge/decisions/.gitkeep
git add -A
git commit -m "Init memory" --quiet
git push -u origin main --quiet 2>/dev/null || git push -u origin HEAD:main --quiet

# Create memory symlink in hub
ln -s "$MEMORY" "$HUB/memory"

echo "[setup] Done."
echo ""

# ============================================================
# TEST 1: session-start.sh shows managed repo status
# ============================================================
echo "[1] Session start shows managed repo status"
cd "$HUB"
git checkout develop --quiet 2>/dev/null
OUTPUT=$(bash bin/session-start.sh 2>/dev/null)

if echo "$OUTPUT" | grep -q "Repos:"; then
  pass "Output contains 'Repos:' section"
else
  fail "Output missing 'Repos:' section"
fi

if echo "$OUTPUT" | grep -q "myapp:"; then
  pass "Output shows myapp repo"
else
  fail "Output missing myapp repo status"
fi

if echo "$OUTPUT" | grep -q "develop"; then
  pass "Output shows develop branch"
else
  fail "Output missing branch info"
fi

# ============================================================
# TEST 2: session-start.sh handles empty repos array
# ============================================================
echo "[2] Session start with empty repos array"
cd "$HUB"
# Temporarily set repos to empty
jq '.repos = []' egregore.json > tmp.json && mv tmp.json egregore.json
OUTPUT_EMPTY=$(bash bin/session-start.sh 2>/dev/null)
EXIT_CODE=$?
# Restore
jq '.repos = ["myapp"]' egregore.json > tmp.json && mv tmp.json egregore.json

if [ "$EXIT_CODE" -eq 0 ]; then
  pass "Exit code 0 with empty repos"
else
  fail "Non-zero exit with empty repos (exit $EXIT_CODE)"
fi

if echo "$OUTPUT_EMPTY" | grep -q "Repos:"; then
  fail "Should NOT show Repos section when array is empty"
else
  pass "No Repos section when array is empty"
fi

# ============================================================
# TEST 3: session-start.sh handles missing sibling directory
# ============================================================
echo "[3] Session start with missing sibling repo"
cd "$HUB"
jq '.repos = ["myapp", "nonexistent"]' egregore.json > tmp.json && mv tmp.json egregore.json
OUTPUT_MISSING=$(bash bin/session-start.sh 2>/dev/null)
EXIT_CODE=$?
jq '.repos = ["myapp"]' egregore.json > tmp.json && mv tmp.json egregore.json

if [ "$EXIT_CODE" -eq 0 ]; then
  pass "Exit code 0 with missing repo"
else
  fail "Non-zero exit with missing repo (exit $EXIT_CODE)"
fi

if echo "$OUTPUT_MISSING" | grep -q "myapp:"; then
  pass "Still shows existing repo (myapp)"
else
  fail "Lost existing repo when one is missing"
fi

if echo "$OUTPUT_MISSING" | grep -q "nonexistent:"; then
  fail "Should NOT show nonexistent repo"
else
  pass "Skips nonexistent repo silently"
fi

# ============================================================
# TEST 4: session-start.sh detects uncommitted changes
# ============================================================
echo "[4] Session start detects dirty managed repo"
cd "$MANAGED_REPO"
echo "new code" >> src/index.js
cd "$HUB"
OUTPUT_DIRTY=$(bash bin/session-start.sh 2>/dev/null)

if echo "$OUTPUT_DIRTY" | grep -q "myapp:.*\*"; then
  pass "Shows * for dirty repo"
else
  fail "Missing dirty indicator (*) for myapp"
fi

# Clean up
cd "$MANAGED_REPO"
git checkout -- src/index.js 2>/dev/null

# ============================================================
# TEST 5: session-start.sh ensures develop branch in managed repo
# ============================================================
echo "[5] Session start ensures develop branch"
cd "$MANAGED_REPO"
# Delete local develop to simulate fresh clone
git checkout main --quiet 2>/dev/null
git branch -D develop --quiet 2>/dev/null || true

cd "$HUB"
bash bin/session-start.sh >/dev/null 2>&1

# Check develop was recreated
cd "$MANAGED_REPO"
if git show-ref --verify --quiet refs/heads/develop 2>/dev/null; then
  pass "Develop branch recreated in managed repo"
else
  fail "Develop branch NOT recreated"
fi

# ============================================================
# TEST 6: git -C branch creation in managed repo
# ============================================================
echo "[6] Branch creation in managed repo via git -C"
REPO_DIR="$MANAGED_REPO"
cd "$HUB"
git -C "$REPO_DIR" fetch origin develop --quiet
git -C "$REPO_DIR" checkout -b dev/testuser/auth-flow origin/develop --quiet 2>/dev/null
BRANCH=$(git -C "$REPO_DIR" branch --show-current)

if [ "$BRANCH" = "dev/testuser/auth-flow" ]; then
  pass "Created dev/testuser/auth-flow in managed repo"
else
  fail "Branch is '$BRANCH', expected 'dev/testuser/auth-flow'"
fi

# ============================================================
# TEST 7: Commit and push in managed repo via git -C
# ============================================================
echo "[7] Commit and push in managed repo via git -C"
cd "$HUB"
echo "// auth middleware" > "$REPO_DIR/src/auth.js"
git -C "$REPO_DIR" add -A
git -C "$REPO_DIR" commit -m "Add auth middleware" --quiet

COMMIT_OK=$?
if [ "$COMMIT_OK" -eq 0 ]; then
  pass "Committed in managed repo"
else
  fail "Commit failed (exit $COMMIT_OK)"
fi

git -C "$REPO_DIR" push -u origin dev/testuser/auth-flow --quiet 2>/dev/null
PUSH_OK=$?
if [ "$PUSH_OK" -eq 0 ]; then
  pass "Pushed branch to managed repo remote"
else
  fail "Push failed (exit $PUSH_OK)"
fi

# ============================================================
# TEST 8: Rebase onto develop in managed repo
# ============================================================
echo "[8] Rebase onto develop in managed repo"
cd "$HUB"

# Add a commit to develop on remote (simulate team activity)
cd "$MANAGED_REPO"
git checkout develop --quiet
echo "// team change" > src/team.js
git add -A
git commit -m "Team change on develop" --quiet
git push origin develop --quiet
git checkout dev/testuser/auth-flow --quiet
cd "$HUB"

# Fetch and rebase
git -C "$REPO_DIR" fetch origin develop --quiet
git -C "$REPO_DIR" rebase origin/develop --quiet 2>/dev/null
REBASE_OK=$?

if [ "$REBASE_OK" -eq 0 ]; then
  pass "Rebase onto develop succeeded"
else
  fail "Rebase failed (exit $REBASE_OK)"
fi

# Verify team change is in history
TEAM_COMMIT=$(git -C "$REPO_DIR" log --oneline | grep "Team change on develop")
if [ -n "$TEAM_COMMIT" ]; then
  pass "Rebased branch includes develop changes"
else
  fail "Develop changes missing after rebase"
fi

# ============================================================
# TEST 9: Multiple repos in parallel fetch
# ============================================================
echo "[9] Multiple repos in egregore.json"
# Create a second managed repo
MANAGED2="$WORKSPACE/backend"
REMOTE_MANAGED2="$WORKSPACE/remotes/backend.git"
git init --bare "$REMOTE_MANAGED2" --quiet
mkdir -p "$MANAGED2"
cd "$MANAGED2"
git init --quiet
git remote add origin "$REMOTE_MANAGED2"
echo "# Backend" > README.md
git add -A
git commit -m "Init backend" --quiet
git push -u origin main --quiet 2>/dev/null || git push -u origin HEAD:main --quiet
git checkout -b develop --quiet
git push -u origin develop --quiet

cd "$HUB"
jq '.repos = ["myapp", "backend"]' egregore.json > tmp.json && mv tmp.json egregore.json
OUTPUT_MULTI=$(bash bin/session-start.sh 2>/dev/null)

if echo "$OUTPUT_MULTI" | grep -q "myapp:" && echo "$OUTPUT_MULTI" | grep -q "backend:"; then
  pass "Both repos shown in status"
else
  fail "Not all repos shown in status"
fi

# Restore
jq '.repos = ["myapp"]' egregore.json > tmp.json && mv tmp.json egregore.json

# ============================================================
# TEST 10: Managed repo without develop branch on remote
# ============================================================
echo "[10] Managed repo without develop on remote"
MANAGED3="$WORKSPACE/simple"
REMOTE_MANAGED3="$WORKSPACE/remotes/simple.git"
git init --bare "$REMOTE_MANAGED3" --quiet
mkdir -p "$MANAGED3"
cd "$MANAGED3"
git init --quiet
git remote add origin "$REMOTE_MANAGED3"
echo "# Simple" > README.md
git add -A
git commit -m "Init" --quiet
git push -u origin main --quiet 2>/dev/null || git push -u origin HEAD:main --quiet
# NO develop branch created

cd "$HUB"
jq '.repos = ["simple"]' egregore.json > tmp.json && mv tmp.json egregore.json
OUTPUT_NODEV=$(bash bin/session-start.sh 2>/dev/null)
EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
  pass "No crash when managed repo has no develop"
else
  fail "Crashed when managed repo has no develop (exit $EXIT_CODE)"
fi

# Restore
jq '.repos = ["myapp"]' egregore.json > tmp.json && mv tmp.json egregore.json

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
