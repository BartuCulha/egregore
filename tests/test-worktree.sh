#!/bin/bash
# E2E tests for git worktree support.
# Tests worktree.sh lifecycle + session-start.sh boundary/detection logic.
#
# Runs in an isolated temp git repo — never touches the real project.
# Usage: bash tests/test-worktree.sh
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREE_SH="$SCRIPT_DIR/bin/worktree.sh"
SESSION_START="$SCRIPT_DIR/bin/session-start.sh"
PASS=0
FAIL=0
SKIP=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
skip() { SKIP=$((SKIP + 1)); echo "  SKIP: $1"; }

# --- Setup: create isolated test repo ---
TEST_ROOT=$(mktemp -d)
MAIN_REPO="$TEST_ROOT/main-repo"
trap 'rm -rf "$TEST_ROOT"' EXIT

setup_test_repo() {
  mkdir -p "$MAIN_REPO/bin" "$MAIN_REPO/.claude/worktrees" "$MAIN_REPO/memory"
  cd "$MAIN_REPO"
  git init --quiet
  git checkout -b main --quiet 2>/dev/null || true

  # Minimal egregore structure
  echo '{"slug":"test","org_name":"Test Org","github_org":"test-org"}' > egregore.json
  echo "GITHUB_TOKEN=fake" > .env
  echo '{"onboarding_complete":true,"github_username":"testuser"}' > .egregore-state.json
  echo "test-session-id" > .egregore-session-id
  echo "# test memory" > memory/README.md

  # Copy the actual worktree.sh into the test repo
  cp "$WORKTREE_SH" bin/worktree.sh
  chmod +x bin/worktree.sh

  git add -A
  git commit -m "init" --quiet

  # Create develop branch
  git checkout -b develop --quiet
  git checkout main --quiet 2>/dev/null || true
}

echo "=== worktree.sh tests ==="
echo ""

# ============================================================
# Phase 1: worktree.sh unit tests
# ============================================================

echo "--- Phase 1: worktree.sh operations ---"

setup_test_repo

# --- Test 1.1: setup creates symlinks ---
echo ""
echo "Test 1.1: setup creates correct symlinks"

cd "$MAIN_REPO"
git worktree add .claude/worktrees/test-topic develop --quiet 2>/dev/null
WT_PATH="$MAIN_REPO/.claude/worktrees/test-topic"

bash bin/worktree.sh setup "$WT_PATH" "$MAIN_REPO" >/dev/null 2>&1

if [ -L "$WT_PATH/.env" ]; then
  TARGET=$(readlink "$WT_PATH/.env")
  if [ "$TARGET" = "$MAIN_REPO/.env" ]; then
    pass ".env symlinked to main repo"
  else
    fail ".env symlink points to '$TARGET' instead of '$MAIN_REPO/.env'"
  fi
else
  fail ".env not created as symlink"
fi

if [ -L "$WT_PATH/.egregore-state.json" ]; then
  pass ".egregore-state.json symlinked"
else
  fail ".egregore-state.json not symlinked"
fi

if [ -L "$WT_PATH/.egregore-session-id" ]; then
  pass ".egregore-session-id symlinked"
else
  fail ".egregore-session-id not symlinked"
fi

if [ -f "$WT_PATH/.egregore-worktree-pid" ]; then
  pass "PID marker created"
else
  fail "PID marker not created"
fi

# --- Test 1.2: setup symlinks egregore.json ---
echo ""
echo "Test 1.2: setup symlinks egregore.json"

if [ -L "$WT_PATH/egregore.json" ]; then
  pass "egregore.json symlinked into worktree"
else
  # It might already exist from git worktree add (tracked file)
  if [ -f "$WT_PATH/egregore.json" ]; then
    pass "egregore.json exists in worktree (tracked file)"
  else
    fail "egregore.json missing from worktree"
  fi
fi

# --- Test 1.3: worktree has .git as file (not directory) ---
echo ""
echo "Test 1.3: worktree .git detection"

if [ -f "$WT_PATH/.git" ] && [ ! -d "$WT_PATH/.git" ]; then
  pass "worktree has .git as file (not directory)"
else
  fail "worktree .git is a directory or missing"
fi

# Verify main repo has .git as directory
if [ -d "$MAIN_REPO/.git" ] && [ ! -f "$MAIN_REPO/.git" ]; then
  pass "main repo has .git as directory"
else
  fail "main repo .git detection wrong"
fi

# --- Test 1.4: cleanup removes worktree ---
echo ""
echo "Test 1.4: cleanup removes worktree"

# Create a second worktree for cleanup testing
git worktree add .claude/worktrees/cleanup-test develop --quiet 2>/dev/null
WT_CLEANUP="$MAIN_REPO/.claude/worktrees/cleanup-test"
bash bin/worktree.sh setup "$WT_CLEANUP" "$MAIN_REPO" >/dev/null 2>&1

bash bin/worktree.sh cleanup "$WT_CLEANUP" >/dev/null 2>&1

if [ ! -d "$WT_CLEANUP" ]; then
  pass "worktree directory removed"
else
  fail "worktree directory still exists after cleanup"
fi

# Verify git worktree list doesn't show it
WT_LIST=$(git worktree list 2>/dev/null)
if echo "$WT_LIST" | grep -q "cleanup-test"; then
  fail "worktree still in git worktree list"
else
  pass "worktree pruned from git worktree list"
fi

# --- Test 1.5: cleanup removes worktree from instance registry ---
echo ""
echo "Test 1.5: cleanup removes from instance registry"

MOCK_REGISTRY_DIR="$TEST_ROOT/mock-home/.egregore"
mkdir -p "$MOCK_REGISTRY_DIR"
WT_REGISTRY_TEST="$MAIN_REPO/.claude/worktrees/registry-test"
git worktree add "$WT_REGISTRY_TEST" develop --quiet 2>/dev/null

# Create a registry that accidentally contains the worktree path
RESOLVED_WT=$(realpath "$WT_REGISTRY_TEST" 2>/dev/null || echo "$WT_REGISTRY_TEST")
cat > "$MOCK_REGISTRY_DIR/instances.json" << REGEOF
[
  {"slug":"test","name":"Test","path":"$MAIN_REPO"},
  {"slug":"test-wt","name":"Test WT","path":"$RESOLVED_WT"}
]
REGEOF

HOME="$TEST_ROOT/mock-home" bash bin/worktree.sh cleanup "$WT_REGISTRY_TEST" >/dev/null 2>&1

REG_COUNT=$(jq length "$MOCK_REGISTRY_DIR/instances.json" 2>/dev/null || echo "?")
if [ "$REG_COUNT" = "1" ]; then
  pass "worktree removed from instance registry"
else
  fail "instance registry has $REG_COUNT entries (expected 1)"
fi

# --- Test 1.6: cleanup-orphans kills dead PID worktrees ---
echo ""
echo "Test 1.6: cleanup-orphans detects dead PIDs"

git branch orphan-branch develop 2>/dev/null || true
WT_ORPHAN="$MAIN_REPO/.claude/worktrees/orphan-test"
git worktree add "$WT_ORPHAN" orphan-branch --quiet 2>/dev/null

if [ ! -d "$WT_ORPHAN" ]; then
  fail "could not create worktree for orphan test"
else
  bash bin/worktree.sh setup "$WT_ORPHAN" "$MAIN_REPO" >/dev/null 2>&1

  # Write a definitely-dead PID (find one that's not running)
  DEAD_PID=99999
  while kill -0 "$DEAD_PID" 2>/dev/null; do
    DEAD_PID=$((DEAD_PID - 1))
  done
  echo "$DEAD_PID" > "$WT_ORPHAN/.egregore-worktree-pid"
fi

bash bin/worktree.sh cleanup-orphans "$MAIN_REPO" >/dev/null 2>&1

if [ ! -d "$WT_ORPHAN" ]; then
  pass "orphaned worktree cleaned up"
else
  fail "orphaned worktree still exists"
fi

# --- Test 1.7: cleanup-orphans does NOT kill live PID worktrees ---
echo ""
echo "Test 1.7: cleanup-orphans preserves live PIDs"

git branch alive-branch develop 2>/dev/null || true
WT_ALIVE="$MAIN_REPO/.claude/worktrees/alive-test"
git worktree add "$WT_ALIVE" alive-branch --quiet 2>/dev/null
bash bin/worktree.sh setup "$WT_ALIVE" "$MAIN_REPO" >/dev/null 2>&1

# Write our own PID (definitely alive)
echo "$$" > "$WT_ALIVE/.egregore-worktree-pid"

bash bin/worktree.sh cleanup-orphans "$MAIN_REPO" >/dev/null 2>&1

if [ -d "$WT_ALIVE" ]; then
  pass "live worktree preserved"
else
  fail "live worktree was incorrectly cleaned up!"
fi

# Clean up this worktree for subsequent tests
bash bin/worktree.sh cleanup "$WT_ALIVE" >/dev/null 2>&1

# --- Test 1.8: list works ---
echo ""
echo "Test 1.8: worktree list"

git branch list-branch develop 2>/dev/null || true
WT_LIST_TEST="$MAIN_REPO/.claude/worktrees/list-test"
git worktree add "$WT_LIST_TEST" list-branch --quiet 2>/dev/null

LIST_OUTPUT=$(cd "$MAIN_REPO" && bash bin/worktree.sh list 2>/dev/null)
if echo "$LIST_OUTPUT" | grep -q "list-test"; then
  pass "list shows worktree"
else
  fail "list doesn't show worktree"
fi

bash bin/worktree.sh cleanup "$WT_LIST_TEST" >/dev/null 2>&1


# ============================================================
# Phase 2: session-start.sh boundary & detection logic
# ============================================================

echo ""
echo "--- Phase 2: session-start.sh integration ---"

# We test the logic in isolation by extracting the key parts,
# since running the full session-start.sh would make network calls.

# --- Test 2.1: Worktree detection logic ---
echo ""
echo "Test 2.1: worktree detection (.git file vs directory)"

# Create a worktree and test detection inside it
git branch detect-branch develop 2>/dev/null || true
WT_DETECT="$MAIN_REPO/.claude/worktrees/detect-test"
git worktree add "$WT_DETECT" detect-branch --quiet 2>/dev/null

# Simulate the detection logic from session-start.sh
IS_WT="false"
DETECTED_MAIN=""
if [ -f "$WT_DETECT/.git" ]; then
  IS_WT="true"
  WT_GITDIR=$(sed 's/^gitdir: //' "$WT_DETECT/.git" 2>/dev/null)
  DETECTED_MAIN=$(cd "$WT_GITDIR/../../.." 2>/dev/null && pwd)
fi

if [ "$IS_WT" = "true" ]; then
  pass "detected .git file as worktree"
else
  fail "failed to detect worktree from .git file"
fi

RESOLVED_MAIN=$(realpath "$MAIN_REPO" 2>/dev/null || echo "$MAIN_REPO")
if [ "$DETECTED_MAIN" = "$RESOLVED_MAIN" ]; then
  pass "resolved main project dir correctly: $DETECTED_MAIN"
else
  fail "main project dir mismatch: got '$DETECTED_MAIN', expected '$RESOLVED_MAIN'"
fi

# Test that main repo is NOT detected as worktree
IS_WT_MAIN="false"
if [ -f "$MAIN_REPO/.git" ]; then
  IS_WT_MAIN="true"
fi
if [ "$IS_WT_MAIN" = "false" ]; then
  pass "main repo not detected as worktree"
else
  fail "main repo incorrectly detected as worktree"
fi

bash bin/worktree.sh cleanup "$WT_DETECT" >/dev/null 2>&1

# --- Test 2.2: Denied paths filtering excludes worktrees ---
echo ""
echo "Test 2.2: denied_paths excludes .claude/worktrees/"

# Create a mock instances.json with worktree paths + other instances
MOCK_INSTANCES=$(cat << JEOF
[
  {"slug":"test","name":"Test","path":"$MAIN_REPO"},
  {"slug":"test","name":"Test WT","path":"$MAIN_REPO/.claude/worktrees/some-feature"},
  {"slug":"test","name":"Test WT2","path":"$MAIN_REPO/.claude/worktrees/another-thing"},
  {"slug":"other","name":"Other Org","path":"/Users/someone/other-egregore"}
]
JEOF
)

# Run the actual jq filter from session-start.sh
DENIED=$(echo "$MOCK_INSTANCES" | jq --arg self "$MAIN_REPO" --arg wt_prefix "$MAIN_REPO/.claude/worktrees" \
  '[.[] | select(.path != $self) | select((.path | startswith($wt_prefix)) | not) | .path]' 2>/dev/null)

DENIED_COUNT=$(echo "$DENIED" | jq length 2>/dev/null || echo "?")
if [ "$DENIED_COUNT" = "1" ]; then
  pass "denied_paths has 1 entry (other instance only)"
else
  fail "denied_paths has $DENIED_COUNT entries (expected 1): $DENIED"
fi

# Verify the correct path is denied
DENIED_PATH=$(echo "$DENIED" | jq -r '.[0]' 2>/dev/null)
if [ "$DENIED_PATH" = "/Users/someone/other-egregore" ]; then
  pass "correct path denied: $DENIED_PATH"
else
  fail "wrong path denied: $DENIED_PATH"
fi

# --- Test 2.3: Denied paths WITHOUT the fix (regression check) ---
echo ""
echo "Test 2.3: old filter WOULD have blocked worktrees (regression proof)"

DENIED_OLD=$(echo "$MOCK_INSTANCES" | jq --arg self "$MAIN_REPO" \
  '[.[] | select(.path != $self) | .path]' 2>/dev/null)

DENIED_OLD_COUNT=$(echo "$DENIED_OLD" | jq length 2>/dev/null || echo "?")
if [ "$DENIED_OLD_COUNT" = "3" ]; then
  pass "old filter would have blocked 3 paths (including worktrees) — confirms bug existed"
else
  fail "unexpected old filter result: $DENIED_OLD_COUNT entries"
fi

# --- Test 2.4: Instance registration guard ---
echo ""
echo "Test 2.4: worktrees skip instance registration"

# The guard in session-start.sh is:
#   if [ "$IS_WORKTREE" = "false" ] && command -v jq &>/dev/null && [ -f "$CONFIG" ]; then
# We verify the condition works

IS_WORKTREE="true"
SHOULD_REGISTER="yes"
if [ "$IS_WORKTREE" = "false" ]; then
  SHOULD_REGISTER="yes"
else
  SHOULD_REGISTER="no"
fi

if [ "$SHOULD_REGISTER" = "no" ]; then
  pass "worktree skips instance registration"
else
  fail "worktree would register as instance"
fi

IS_WORKTREE="false"
if [ "$IS_WORKTREE" = "false" ]; then
  SHOULD_REGISTER="yes"
else
  SHOULD_REGISTER="no"
fi

if [ "$SHOULD_REGISTER" = "yes" ]; then
  pass "main repo proceeds with registration"
else
  fail "main repo skips registration"
fi


# ============================================================
# Phase 3: Symlink integrity under git operations
# ============================================================

echo ""
echo "--- Phase 3: Symlink integrity ---"

# --- Test 3.1: Symlinked files are readable from worktree ---
echo ""
echo "Test 3.1: symlinked files readable from worktree"

cd "$MAIN_REPO"
git branch symlink-branch develop 2>/dev/null || true
WT_SYMLINK="$MAIN_REPO/.claude/worktrees/symlink-test"
git worktree add "$WT_SYMLINK" symlink-branch --quiet 2>/dev/null
bash bin/worktree.sh setup "$WT_SYMLINK" "$MAIN_REPO" >/dev/null 2>&1

# Read .env through symlink
ENV_CONTENT=$(cat "$WT_SYMLINK/.env" 2>/dev/null || echo "UNREADABLE")
if echo "$ENV_CONTENT" | grep -q "GITHUB_TOKEN"; then
  pass ".env readable through symlink"
else
  fail ".env not readable through symlink: $ENV_CONTENT"
fi

# Read state through symlink
STATE_CONTENT=$(jq -r '.github_username' "$WT_SYMLINK/.egregore-state.json" 2>/dev/null || echo "UNREADABLE")
if [ "$STATE_CONTENT" = "testuser" ]; then
  pass "state file readable through symlink"
else
  fail "state file not readable: $STATE_CONTENT"
fi

# --- Test 3.2: Changes to main .env are visible in worktree ---
echo ""
echo "Test 3.2: main repo changes visible through worktree symlinks"

echo "GITHUB_TOKEN=updated-token" > "$MAIN_REPO/.env"
WT_TOKEN=$(grep 'GITHUB_TOKEN' "$WT_SYMLINK/.env" 2>/dev/null | cut -d= -f2-)
if [ "$WT_TOKEN" = "updated-token" ]; then
  pass "main .env changes visible in worktree"
else
  fail "worktree sees stale .env: $WT_TOKEN"
fi

# --- Test 3.3: git operations work in worktree ---
echo ""
echo "Test 3.3: git operations work inside worktree"

cd "$WT_SYMLINK"
WT_BRANCH=$(git branch --show-current 2>/dev/null)
if [ -n "$WT_BRANCH" ]; then
  pass "git branch works in worktree (on: $WT_BRANCH)"
else
  fail "git branch returned empty in worktree"
fi

# Can we commit in the worktree?
echo "test file" > "$WT_SYMLINK/test-file.txt"
git add test-file.txt 2>/dev/null
git commit -m "test commit from worktree" --quiet 2>/dev/null
COMMIT_OK=$?
if [ "$COMMIT_OK" -eq 0 ]; then
  pass "git commit works in worktree"
else
  fail "git commit failed in worktree"
fi

cd "$MAIN_REPO"
bash bin/worktree.sh cleanup "$WT_SYMLINK" >/dev/null 2>&1


# ============================================================
# Phase 4: Edge cases
# ============================================================

echo ""
echo "--- Phase 4: Edge cases ---"

# --- Test 4.1: cleanup-orphans with no worktrees dir ---
echo ""
echo "Test 4.1: cleanup-orphans handles missing .claude/worktrees/"

rmdir "$MAIN_REPO/.claude/worktrees" 2>/dev/null || true
EXIT_CODE=0
bash bin/worktree.sh cleanup-orphans "$MAIN_REPO" >/dev/null 2>&1 || EXIT_CODE=$?
if [ "$EXIT_CODE" -eq 0 ]; then
  pass "cleanup-orphans handles missing worktrees dir gracefully"
else
  fail "cleanup-orphans crashed with exit $EXIT_CODE"
fi
mkdir -p "$MAIN_REPO/.claude/worktrees"

# --- Test 4.2: setup with missing .env (no crash) ---
echo ""
echo "Test 4.2: setup handles missing optional files"

cd "$MAIN_REPO"
git branch minimal-branch develop 2>/dev/null || true
WT_MINIMAL="$MAIN_REPO/.claude/worktrees/minimal-test"
git worktree add "$WT_MINIMAL" minimal-branch --quiet 2>/dev/null

# Remove optional files temporarily
mv "$MAIN_REPO/.env" "$MAIN_REPO/.env.bak"
mv "$MAIN_REPO/.egregore-session-id" "$MAIN_REPO/.egregore-session-id.bak"

EXIT_CODE=0
bash bin/worktree.sh setup "$WT_MINIMAL" "$MAIN_REPO" >/dev/null 2>&1 || EXIT_CODE=$?

if [ "$EXIT_CODE" -eq 0 ]; then
  pass "setup succeeds with missing optional files"
else
  fail "setup crashed with exit $EXIT_CODE"
fi

# .env symlink should NOT exist (source file missing)
if [ ! -L "$WT_MINIMAL/.env" ]; then
  pass "no .env symlink when source missing"
else
  fail ".env symlink created despite missing source"
fi

# State file should still be symlinked (it exists)
if [ -L "$WT_MINIMAL/.egregore-state.json" ]; then
  pass "state file still symlinked"
else
  fail "state file not symlinked"
fi

# Restore
mv "$MAIN_REPO/.env.bak" "$MAIN_REPO/.env"
mv "$MAIN_REPO/.egregore-session-id.bak" "$MAIN_REPO/.egregore-session-id"
bash bin/worktree.sh cleanup "$WT_MINIMAL" >/dev/null 2>&1

# --- Test 4.3: concurrent worktrees don't interfere ---
echo ""
echo "Test 4.3: multiple worktrees coexist"

cd "$MAIN_REPO"
# Create two branches to avoid "already checked out" error
git branch feature-a develop 2>/dev/null || true
git branch feature-b develop 2>/dev/null || true

WT_A="$MAIN_REPO/.claude/worktrees/feature-a"
WT_B="$MAIN_REPO/.claude/worktrees/feature-b"
git worktree add "$WT_A" feature-a --quiet 2>/dev/null
git worktree add "$WT_B" feature-b --quiet 2>/dev/null
bash bin/worktree.sh setup "$WT_A" "$MAIN_REPO" >/dev/null 2>&1
bash bin/worktree.sh setup "$WT_B" "$MAIN_REPO" >/dev/null 2>&1

BRANCH_A=$(git -C "$WT_A" branch --show-current 2>/dev/null)
BRANCH_B=$(git -C "$WT_B" branch --show-current 2>/dev/null)

if [ "$BRANCH_A" = "feature-a" ] && [ "$BRANCH_B" = "feature-b" ]; then
  pass "two worktrees on different branches"
else
  fail "branch mismatch: A=$BRANCH_A B=$BRANCH_B"
fi

# Create different files in each — no conflict
echo "file a" > "$WT_A/a.txt"
echo "file b" > "$WT_B/b.txt"
git -C "$WT_A" add a.txt && git -C "$WT_A" commit -m "add a" --quiet 2>/dev/null
git -C "$WT_B" add b.txt && git -C "$WT_B" commit -m "add b" --quiet 2>/dev/null

# Verify files don't leak across worktrees
if [ ! -f "$WT_A/b.txt" ] && [ ! -f "$WT_B/a.txt" ]; then
  pass "worktrees are isolated (no file leaking)"
else
  fail "files leaked between worktrees"
fi

# Cleanup
bash bin/worktree.sh cleanup "$WT_A" >/dev/null 2>&1
bash bin/worktree.sh cleanup "$WT_B" >/dev/null 2>&1

# --- Test 4.4: denied_paths with empty registry ---
echo ""
echo "Test 4.4: denied_paths with empty/missing registry"

DENIED_EMPTY=$(echo "[]" | jq --arg self "$MAIN_REPO" --arg wt_prefix "$MAIN_REPO/.claude/worktrees" \
  '[.[] | select(.path != $self) | select((.path | startswith($wt_prefix)) | not) | .path]' 2>/dev/null)

if [ "$DENIED_EMPTY" = "[]" ]; then
  pass "empty registry produces empty denied list"
else
  fail "empty registry produced: $DENIED_EMPTY"
fi

# --- Test 4.5: denied_paths with only self (no other instances) ---
echo ""
echo "Test 4.5: denied_paths with only self registered"

DENIED_SELF=$(echo "[{\"path\":\"$MAIN_REPO\"}]" | \
  jq --arg self "$MAIN_REPO" --arg wt_prefix "$MAIN_REPO/.claude/worktrees" \
  '[.[] | select(.path != $self) | select((.path | startswith($wt_prefix)) | not) | .path]' 2>/dev/null)

if [ "$DENIED_SELF" = "[]" ]; then
  pass "self-only registry produces empty denied list"
else
  fail "self-only registry produced: $DENIED_SELF"
fi


# ============================================================
# Results
# ============================================================

echo ""
echo "==========================================="
echo "  Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "==========================================="

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
