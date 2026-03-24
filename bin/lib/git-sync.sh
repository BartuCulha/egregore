# git-sync.sh — Git synchronization for session-start.sh
#
# Handles all git operations during session startup:
#   - Pull rebase configuration
#   - Parallel fetches (origin, upstream, memory, managed repos)
#   - Worktree orphan cleanup
#   - Git health check
#   - Develop branch setup + sync
#   - Memory sync
#   - Managed repos sync
#
# Inputs:  SCRIPT_DIR, IS_WORKTREE, MAIN_PROJECT_DIR, STATE_FILE, ENV_FILE,
#          HEALTH_GIT, MANAGED_REPOS (not yet set — read from config here)
# Outputs: HEALTH_GIT, DEVELOP_SYNCED, COMMITS_AHEAD, ACTION, SAVED_BRANCH,
#          BRANCH, MEMORY_SYNCED, REPOS_STATUS, MANAGED_REPOS, CURRENT_BRANCH

# --- Ensure pull.rebase is set (prevents "divergent branches" errors) ---
git config pull.rebase true 2>/dev/null || true

# --- Fetch all remotes in parallel ---
# Set git transfer timeout so hangs don't block startup indefinitely.
export GIT_HTTP_LOW_SPEED_LIMIT=1000  # abort if <1KB/s
export GIT_HTTP_LOW_SPEED_TIME=10     # for 10 seconds

git fetch origin --quiet 2>/dev/null &

# Fetch upstream framework (for update check — non-blocking)
# Default: official egregore-core repo. Set upstream_url in egregore.json to override,
# or set it to "none" to disable (e.g., in the dev repo where this IS the source).
_UPSTREAM_URL=$(jq -r '.upstream_url // empty' "$SCRIPT_DIR/egregore.json" 2>/dev/null)
[ -z "$_UPSTREAM_URL" ] && _UPSTREAM_URL="https://github.com/Curve-Labs/egregore-core.git"
if [ "$_UPSTREAM_URL" != "none" ]; then
  git remote add upstream "$_UPSTREAM_URL" 2>/dev/null || true
  git fetch upstream main --quiet 2>/dev/null &
fi

# Sync memory in parallel
MEMORY_SYNCED="false"
if [ -L "$SCRIPT_DIR/memory" ] && [ -d "$SCRIPT_DIR/memory/.git" ]; then
  git -C "$SCRIPT_DIR/memory" fetch origin --quiet 2>/dev/null &
fi

# Fetch managed repos in parallel
MANAGED_REPOS=$(jq -r '.repos[]? // empty' "$SCRIPT_DIR/egregore.json" 2>/dev/null)
for REPO in $MANAGED_REPOS; do
  if [ -d "$SCRIPT_DIR/../$REPO/.git" ]; then
    git -C "$SCRIPT_DIR/../$REPO" fetch origin --quiet 2>/dev/null &
  fi
done

# Wait for all fetches
wait 2>/dev/null || true

# --- Worktree orphan cleanup (background, throttled to max once/hour) ---
_CLEANUP_MARKER="/tmp/egregore-cleanup-last-$(echo -n "$SCRIPT_DIR" | md5 2>/dev/null || echo -n "$SCRIPT_DIR" | md5sum 2>/dev/null | cut -d' ' -f1)"
_RUN_CLEANUP="false"
if [ ! -f "$_CLEANUP_MARKER" ]; then
  _RUN_CLEANUP="true"
else
  _CM_MTIME=$(stat -f %m "$_CLEANUP_MARKER" 2>/dev/null || stat -c %Y "$_CLEANUP_MARKER" 2>/dev/null || echo "0")
  _CM_NOW=$(date +%s)
  [ $((_CM_NOW - _CM_MTIME)) -gt 3600 ] 2>/dev/null && _RUN_CLEANUP="true"
fi
if [ "$_RUN_CLEANUP" = "true" ]; then
  touch "$_CLEANUP_MARKER" 2>/dev/null || true
  bash "$SCRIPT_DIR/bin/worktree.sh" cleanup-orphans "$SCRIPT_DIR" 2>/dev/null &
fi

# --- Clean up stale worktree cleanup markers (from crashed sessions) ---
(
  _MK_NOW=$(date +%s)
  for MARKER_FILE in "$HOME/.egregore"/worktree-cleanup-*.marker; do
    [ -f "$MARKER_FILE" ] || continue
    WT_MARKER_PATH=$(cat "$MARKER_FILE" 2>/dev/null)
    if [ -n "$WT_MARKER_PATH" ] && [ -d "$WT_MARKER_PATH" ]; then
      # Skip worktrees younger than 1 hour
      _MK_MTIME=$(stat -f %m "$WT_MARKER_PATH" 2>/dev/null || stat -c %Y "$WT_MARKER_PATH" 2>/dev/null || echo "$_MK_NOW")
      _MK_AGE=$((_MK_NOW - _MK_MTIME))
      if [ "$_MK_AGE" -lt 3600 ] 2>/dev/null; then
        continue
      fi
      PID_FILE="$WT_MARKER_PATH/.egregore-worktree-pid"
      if [ -f "$PID_FILE" ]; then
        STORED_PID=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$STORED_PID" ] && kill -0 "$STORED_PID" 2>/dev/null; then
          continue  # PID alive — another session still using this worktree
        fi
      fi
      bash "$SCRIPT_DIR/bin/worktree.sh" cleanup "$WT_MARKER_PATH" 2>/dev/null || true
    fi
    rm -f "$MARKER_FILE" 2>/dev/null || true
  done
) 2>/dev/null &

# --- Git health check ---
if git show-ref --verify --quiet refs/remotes/origin/develop 2>/dev/null; then
  HEALTH_GIT="ok"
else
  HEALTH_GIT="fail"
fi

# --- Setup develop (wrapped — failures set HEALTH_GIT but don't crash) ---
setup_develop() {
  # Ensure develop branch exists locally
  if ! git show-ref --verify --quiet refs/heads/develop 2>/dev/null; then
    if git show-ref --verify --quiet refs/remotes/origin/develop 2>/dev/null; then
      git checkout -b develop origin/develop --quiet 2>/dev/null
    else
      # No develop on remote either — create from main
      git checkout -b develop --quiet 2>/dev/null
      git push -u origin develop --quiet 2>/dev/null
    fi
  fi

  # Sync develop (without checkout — safe for concurrent sessions)
  CURRENT_BRANCH=$(git branch --show-current)

  # Update local develop ref from remote without switching branches
  # Use fetch + force-update to handle divergence (e.g., from auto-update commits)
  git fetch origin develop --quiet 2>/dev/null || true
  if [[ "$CURRENT_BRANCH" != "develop" ]]; then
    # Safe to force-update when not checked out
    git branch -f develop origin/develop 2>/dev/null || true
  else
    # On develop — try ff merge, fall back to reset only if no unpushed work
    git merge --ff-only origin/develop --quiet 2>/dev/null || {
      # Only reset if there are no unpushed commits on develop
      LOCAL_AHEAD=$(git rev-list origin/develop..develop --count 2>/dev/null || echo "0")
      if [ "$LOCAL_AHEAD" = "0" ]; then
        git reset --hard origin/develop --quiet 2>/dev/null || true
      fi
    }
  fi
  DEVELOP_SYNCED="true"

  # Count commits on develop ahead of main
  COMMITS_AHEAD=0
  if git show-ref --verify --quiet refs/remotes/origin/main 2>/dev/null; then
    COMMITS_AHEAD=$(git rev-list origin/main..develop --count 2>/dev/null || echo "0")
  fi

  # Always start from develop
  # Every session begins on develop. Claude creates a fresh topic branch
  # (dev/{author}/{topic-slug}) when the user says what they're working on.
  # If we're on a working branch with uncommitted work, save it first.
  ACTION="ready"
  SAVED_BRANCH=""

  if [[ "$CURRENT_BRANCH" != "develop" ]]; then
    # Check for uncommitted work (staged + unstaged + untracked in tracked dirs)
    if [ -n "$(git status --porcelain 2>/dev/null | head -1)" ]; then
      # Save uncommitted work so nothing is lost
      git add -A 2>/dev/null
      git commit -m "Auto-save: uncommitted work from $CURRENT_BRANCH" --quiet 2>/dev/null || true
      SAVED_BRANCH="$CURRENT_BRANCH"
    fi

    # Push the branch if it has unpushed commits (so work is never only local)
    if [[ "$CURRENT_BRANCH" == dev/* ]] || [[ "$CURRENT_BRANCH" == feature/* ]] || [[ "$CURRENT_BRANCH" == bugfix/* ]]; then
      LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
      REMOTE_HEAD=$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || echo "none")
      if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
        git push origin "$CURRENT_BRANCH" --quiet 2>/dev/null || true
      fi
    fi

    # Switch to develop (already updated by fetch origin develop:develop above)
    git checkout develop --quiet 2>/dev/null || true
  else
    # Already on develop — fetch couldn't update it (checked-out branch), so pull
    git merge --ff-only origin/develop --quiet 2>/dev/null || true
  fi

  BRANCH="develop"
}

# Initialize defaults used by setup_develop
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
DEVELOP_SYNCED="false"
COMMITS_AHEAD=0
ACTION="ready"
SAVED_BRANCH=""
BRANCH="${CURRENT_BRANCH:-develop}"

if [ "$IS_WORKTREE" = "true" ]; then
  # Inside a worktree — skip develop checkout, we're already on our branch
  BRANCH=$(git branch --show-current 2>/dev/null || echo "?")
  DEVELOP_SYNCED="true"

  # --- Worktree branch health check ---
  # Detect if the branch was already merged into develop (e.g., PR merged
  # but worktree not cleaned up). This prevents working on a stale branch
  # whose commits are already in develop.
  WORKTREE_STALE="false"
  if [ "$BRANCH" != "?" ] && [ "$BRANCH" != "develop" ] && [ "$BRANCH" != "main" ]; then
    # Check if remote branch still exists
    if ! git ls-remote --heads origin "$BRANCH" 2>/dev/null | grep -q "$BRANCH"; then
      # Remote branch gone — check if it was merged into develop
      if git branch -r --merged origin/develop 2>/dev/null | grep -q "origin/$BRANCH" 2>/dev/null || \
         git merge-base --is-ancestor HEAD origin/develop 2>/dev/null; then
        WORKTREE_STALE="merged"
      else
        WORKTREE_STALE="remote_deleted"
      fi
    fi
  fi

  if [ "$WORKTREE_STALE" != "false" ]; then
    echo ""
    echo "WARNING: Branch '$BRANCH' was already merged into develop."
    echo "This worktree is stale — your work here is already in develop."
    echo "Start a new session in the main project to get a fresh branch."
    echo ""
    HEALTH_GIT="fail"
  fi

  # Use main project's .env and state if ours are missing
  if [ ! -f "$SCRIPT_DIR/.env" ] && [ -f "$MAIN_PROJECT_DIR/.env" ]; then
    export ENV_FILE="$MAIN_PROJECT_DIR/.env"
  fi
elif ! setup_develop 2>/dev/null; then
  HEALTH_GIT="fail"
fi

# --- Sync memory ---
if [ -L "$SCRIPT_DIR/memory" ] && [ -d "$SCRIPT_DIR/memory/.git" ]; then
  MEM_LOCAL=$(git -C "$SCRIPT_DIR/memory" rev-parse HEAD 2>/dev/null || echo "")
  MEM_REMOTE=$(git -C "$SCRIPT_DIR/memory" rev-parse origin/main 2>/dev/null || echo "")
  if [ -n "$MEM_LOCAL" ] && [ -n "$MEM_REMOTE" ] && [ "$MEM_LOCAL" != "$MEM_REMOTE" ]; then
    git -C "$SCRIPT_DIR/memory" pull origin main --quiet 2>/dev/null || true
  fi
  MEMORY_SYNCED="true"
fi

# --- Sync managed repos ---
REPOS_STATUS=""
for REPO in $MANAGED_REPOS; do
  REPO_DIR="$SCRIPT_DIR/../$REPO"
  if [ -d "$REPO_DIR/.git" ]; then
    # Ensure develop branch exists locally
    if ! git -C "$REPO_DIR" show-ref --verify --quiet refs/heads/develop 2>/dev/null; then
      if git -C "$REPO_DIR" show-ref --verify --quiet refs/remotes/origin/develop 2>/dev/null; then
        git -C "$REPO_DIR" branch develop origin/develop --quiet 2>/dev/null || true
      fi
    else
      git -C "$REPO_DIR" fetch origin develop:develop --quiet 2>/dev/null || true
    fi
    # Collect status
    R_BRANCH=$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || echo "?")
    R_DIRTY=""
    if [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null | head -1)" ]; then
      R_DIRTY=" *"
    fi
    REPOS_STATUS="${REPOS_STATUS}  ◇ ${REPO}: ${R_BRANCH}${R_DIRTY}\n"
  fi
done
