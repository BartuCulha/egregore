#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$SCRIPT_DIR/egregore.json"

if [ ! -f "$CONFIG" ]; then
  echo "Error: egregore.json not found. Run onboarding first." >&2
  exit 1
fi

# Read token from .env
if [ -f "$SCRIPT_DIR/.env" ]; then
  GITHUB_TOKEN=$(grep '^GITHUB_TOKEN=' "$SCRIPT_DIR/.env" | cut -d'=' -f2-)
else
  echo "Error: .env not found. Run onboarding first." >&2
  exit 1
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "Error: GITHUB_TOKEN not set in .env" >&2
  exit 1
fi

# Config
TARGET_REPO="Curve-Labs/egregore-site"
SOURCE_DIR="$SCRIPT_DIR/site"
DRY_RUN=false
PREVIEW=false
TARGET_BRANCH="main"

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --preview)
      PREVIEW=true
      TARGET_BRANCH="preview"
      shift
      ;;
    --dry-run|dry)
      DRY_RUN=true
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: deploy-site.sh [--preview] [--dry-run|dry]" >&2
      exit 1
      ;;
  esac
done

# Verify local site/ directory exists
if [ ! -d "$SOURCE_DIR" ]; then
  echo "Error: site/ directory not found at $SOURCE_DIR" >&2
  exit 1
fi

WORK_DIR="/tmp/egregore-deploy"
TARGET_PATH="$WORK_DIR/target"

cleanup() {
  rm -rf "$WORK_DIR"
}

mkdir -p "$WORK_DIR"

# Clone or pull target repo
echo "Fetching target: $TARGET_REPO..." >&2

if [ -d "$TARGET_PATH/.git" ]; then
  git -C "$TARGET_PATH" fetch origin --quiet
  # Checkout target branch, create if it doesn't exist
  if git -C "$TARGET_PATH" rev-parse --verify "origin/$TARGET_BRANCH" >/dev/null 2>&1; then
    git -C "$TARGET_PATH" checkout "$TARGET_BRANCH" --quiet 2>/dev/null || git -C "$TARGET_PATH" checkout -b "$TARGET_BRANCH" "origin/$TARGET_BRANCH" --quiet
    git -C "$TARGET_PATH" pull origin "$TARGET_BRANCH" --quiet
  else
    # Branch doesn't exist remotely — create from main
    git -C "$TARGET_PATH" checkout main --quiet
    git -C "$TARGET_PATH" pull origin main --quiet
    git -C "$TARGET_PATH" checkout -b "$TARGET_BRANCH" --quiet 2>/dev/null || git -C "$TARGET_PATH" checkout "$TARGET_BRANCH" --quiet
  fi
else
  rm -rf "$TARGET_PATH"
  git clone --quiet \
    "https://$GITHUB_TOKEN@github.com/$TARGET_REPO.git" "$TARGET_PATH"
  # Checkout target branch
  if [ "$TARGET_BRANCH" != "main" ]; then
    if git -C "$TARGET_PATH" rev-parse --verify "origin/$TARGET_BRANCH" >/dev/null 2>&1; then
      git -C "$TARGET_PATH" checkout -b "$TARGET_BRANCH" "origin/$TARGET_BRANCH" --quiet
    else
      git -C "$TARGET_PATH" checkout -b "$TARGET_BRANCH" --quiet
    fi
  fi
fi

# Sync site contents from local source
echo "Syncing files from local site/..." >&2
rsync -a --delete \
  --exclude='.git' \
  --exclude='.git/' \
  --exclude='node_modules' \
  --exclude='node_modules/' \
  --exclude='dist' \
  --exclude='dist/' \
  --exclude='package-lock.json' \
  "$SOURCE_DIR/" "$TARGET_PATH/"

# Check for changes
cd "$TARGET_PATH"
if [ -z "$(git status --porcelain)" ]; then
  echo "RESULT:no_changes"
  cleanup
  exit 0
fi

# Show what changed
CHANGED=$(git status --porcelain | wc -l | tr -d ' ')
ADDED=$(git status --porcelain | grep -c '^?' || true)
MODIFIED=$(git status --porcelain | grep -c '^ M\|^M' || true)
DELETED=$(git status --porcelain | grep -c '^ D\|^D' || true)

echo "FILES_CHANGED:$CHANGED" >&2
echo "FILES_ADDED:$ADDED" >&2
echo "FILES_MODIFIED:$MODIFIED" >&2
echo "FILES_DELETED:$DELETED" >&2

if [ "$DRY_RUN" = true ]; then
  echo "" >&2
  echo "=== Dry run — changes that would be deployed ===" >&2
  git status --short >&2
  echo "" >&2
  git diff --stat >&2
  echo "RESULT:dry_run"
  cleanup
  exit 0
fi

# Commit and push
DEPLOY_DATE=$(date +%Y-%m-%d)
DEPLOY_MSG="Deploy site: $DEPLOY_DATE"
if [ "$PREVIEW" = true ]; then
  DEPLOY_MSG="Preview deploy: $DEPLOY_DATE"
fi

git add -A
git commit -m "$DEPLOY_MSG" --quiet
git push origin "$TARGET_BRANCH" --quiet

COMMIT_SHA=$(git rev-parse --short HEAD)

if [ "$PREVIEW" = true ]; then
  echo "RESULT:preview:$COMMIT_SHA"
else
  echo "RESULT:deployed:$COMMIT_SHA"
fi

cleanup
exit 0
