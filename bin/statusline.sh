#!/bin/bash
# Egregore statusline — shows branch + worktree + unsaved changes count.
# Reads CC JSON from stdin for worktree awareness.
# Runs on every assistant turn. Must be fast (<100ms).
set -euo pipefail

input=$(cat)

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Worktree detection from CC JSON input
WT_NAME=$(echo "$input" | jq -r '.worktree.name // empty' 2>/dev/null)

# Branch — try JSON first, fall back to git
BRANCH=$(echo "$input" | jq -r '.worktree.branch // empty' 2>/dev/null)
if [ -z "$BRANCH" ]; then
  BRANCH=$(git -C "$SCRIPT_DIR" branch --show-current 2>/dev/null || echo "?")
fi

# Count modified/untracked files (fast — no status porcelain)
CHANGED=$(git -C "$SCRIPT_DIR" diff --name-only 2>/dev/null | wc -l | tr -d ' ')
STAGED=$(git -C "$SCRIPT_DIR" diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
TOTAL=$((CHANGED + STAGED))

# Build output
OUT="⎇ $BRANCH"
if [ -n "$WT_NAME" ]; then
  OUT="$OUT · wt:$WT_NAME"
fi
if [ "$TOTAL" -gt 0 ]; then
  OUT="$OUT · $TOTAL unsaved"
fi

echo "$OUT"
