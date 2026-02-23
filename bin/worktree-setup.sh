#!/bin/bash
# WorktreeCreate hook — REPLACES default git worktree creation.
# Receives JSON on stdin with {name, session_id, cwd, ...}.
# Must: create the worktree, fix symlinks, print absolute path to stdout.
# Any non-zero exit fails worktree creation.
# All non-path output must go to stderr (stdout is parsed by Claude Code).
#
# IMPORTANT: Do NOT use set -euo pipefail — jq failures or missing
# fields must not crash the entire hook. Handle errors explicitly.

# Read hook input from stdin (all hooks receive JSON)
HOOK_INPUT=$(cat 2>/dev/null || true)
NAME=$(echo "$HOOK_INPUT" | jq -r '.name // empty' 2>/dev/null || true)
CWD=$(echo "$HOOK_INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

if [ -z "$NAME" ]; then
  echo "WorktreeCreate: no name in hook input" >&2
  exit 1
fi

# Resolve repo root from cwd (Claude sets cwd to the project directory)
REPO_ROOT="${CWD:-$(pwd)}"
if [ ! -d "$REPO_ROOT/.git" ]; then
  echo "WorktreeCreate: not a git repo root: $REPO_ROOT" >&2
  exit 1
fi

# Create worktree directory
WORKTREE_DIR="$REPO_ROOT/.claude/worktrees/$NAME"
mkdir -p "$(dirname "$WORKTREE_DIR")" 2>/dev/null || true

# Create git worktree (we replace the default behavior)
if ! git -C "$REPO_ROOT" worktree add "$WORKTREE_DIR" --detach --quiet >/dev/null 2>/dev/null; then
  echo "WorktreeCreate: git worktree add failed for $WORKTREE_DIR" >&2
  exit 1
fi

# --- Fix symlinks for gitignored files ---

# 1. Memory symlink (relative → absolute)
if [ -L "$REPO_ROOT/memory" ]; then
  MEMORY_TARGET=$(realpath "$REPO_ROOT/memory" 2>/dev/null || true)
  if [ -n "$MEMORY_TARGET" ] && [ -d "$MEMORY_TARGET" ]; then
    rm -f "$WORKTREE_DIR/memory" 2>/dev/null || true
    ln -sf "$MEMORY_TARGET" "$WORKTREE_DIR/memory"
  fi
fi

# 2. .env (secrets — shared, not copied)
if [ -f "$REPO_ROOT/.env" ] && [ ! -f "$WORKTREE_DIR/.env" ]; then
  ln -sf "$REPO_ROOT/.env" "$WORKTREE_DIR/.env"
fi

# 3. .egregore-state.json (user state)
if [ -f "$REPO_ROOT/.egregore-state.json" ] && [ ! -f "$WORKTREE_DIR/.egregore-state.json" ]; then
  ln -sf "$REPO_ROOT/.egregore-state.json" "$WORKTREE_DIR/.egregore-state.json"
fi

# 4. .egregore/ dir (notes, eval-runs, context.md)
if [ -d "$REPO_ROOT/.egregore" ] && [ ! -d "$WORKTREE_DIR/.egregore" ]; then
  ln -sf "$REPO_ROOT/.egregore" "$WORKTREE_DIR/.egregore"
fi

# --- Signal worktree path to SessionStart hook ---
# Both hooks share the same session_id. Write to /tmp so SessionStart can read it.
if [ -n "$SESSION_ID" ]; then
  echo "$WORKTREE_DIR" > "/tmp/egregore-worktree-$SESSION_ID" 2>/dev/null || true
fi

# Print the absolute path — ONLY this line goes to stdout
echo "$WORKTREE_DIR"
