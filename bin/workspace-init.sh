#!/bin/bash
# Egregore Coder Workspace Init
# Called by Coder agent on workspace start.
# Clones repos, writes config, sets up memory symlink.
# Idempotent — safe to run on every workspace restart.
set -euo pipefail

HOME_DIR="$HOME"
EGREGORE_DIR="$HOME_DIR/egregore"
MEMORY_DIR="$HOME_DIR/memory"

# Required env vars (injected by Coder template)
: "${ORG_SLUG:?ORG_SLUG not set}"
: "${GITHUB_ORG:?GITHUB_ORG not set}"

# ─── Git credential setup ────────────────────────────────────────

if [ -n "${GITHUB_TOKEN:-}" ]; then
  git config --global credential.helper store
  echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > "$HOME_DIR/.git-credentials"
  chmod 600 "$HOME_DIR/.git-credentials"

  # Set git identity from GitHub API (cached after first run)
  if [ ! -f "$HOME_DIR/.egregore/.git-identity-set" ]; then
    GH_USER=$(curl -sf -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user --max-time 5 2>/dev/null || echo '{}')
    GH_NAME=$(echo "$GH_USER" | jq -r '.name // .login // "egregore"')
    GH_LOGIN=$(echo "$GH_USER" | jq -r '.login // "egregore"')
    git config --global user.name "$GH_NAME"
    git config --global user.email "${GH_LOGIN}@users.noreply.github.com"
    mkdir -p "$HOME_DIR/.egregore"
    touch "$HOME_DIR/.egregore/.git-identity-set"
  fi
fi

# ─── Clone or update Egregore repo ───────────────────────────────

REPO_NAME="${REPO_NAME:-egregore-core}"
FORK_URL="${FORK_URL:-https://github.com/${GITHUB_ORG}/${REPO_NAME}.git}"

if [ ! -d "$EGREGORE_DIR/.git" ]; then
  echo "Cloning Egregore repo..."
  git clone "$FORK_URL" "$EGREGORE_DIR"
else
  echo "Updating Egregore repo..."
  cd "$EGREGORE_DIR"
  git fetch origin --quiet 2>/dev/null || true
  CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
  if [ "$CURRENT_BRANCH" = "develop" ] || [ "$CURRENT_BRANCH" = "main" ]; then
    git pull --ff-only origin "$CURRENT_BRANCH" 2>/dev/null || true
  fi
fi

# ─── Clone or update memory repo ─────────────────────────────────

MEMORY_URL="${MEMORY_URL:-}"
if [ -n "$MEMORY_URL" ]; then
  if [ ! -d "$MEMORY_DIR/.git" ]; then
    echo "Cloning memory repo..."
    git clone "$MEMORY_URL" "$MEMORY_DIR"
  else
    echo "Updating memory repo..."
    cd "$MEMORY_DIR"
    git pull --ff-only origin main 2>/dev/null || true
  fi

  # Create memory symlink (idempotent)
  ln -sfn "$MEMORY_DIR" "$EGREGORE_DIR/memory"
fi

# ─── Clone managed repos ─────────────────────────────────────────

MANAGED_REPOS="${MANAGED_REPOS:-}"
if [ -n "$MANAGED_REPOS" ]; then
  IFS=',' read -ra REPOS <<< "$MANAGED_REPOS"
  for repo in "${REPOS[@]}"; do
    repo=$(echo "$repo" | xargs) # trim whitespace
    [ -z "$repo" ] && continue
    REPO_DIR="$HOME_DIR/$repo"
    if [ ! -d "$REPO_DIR/.git" ]; then
      echo "Cloning managed repo: $repo"
      git clone "https://github.com/${GITHUB_ORG}/$repo.git" "$REPO_DIR" 2>/dev/null || echo "Warning: could not clone $repo"
    else
      echo "Updating managed repo: $repo"
      cd "$REPO_DIR"
      git fetch origin --quiet 2>/dev/null || true
    fi
  done
fi

# ─── Write egregore.json ─────────────────────────────────────────

ORG_NAME="${ORG_NAME:-Egregore}"
API_URL="${API_URL:-https://egregore-production-55f2.up.railway.app}"

# Build managed repos JSON array
MANAGED_JSON="[]"
if [ -n "$MANAGED_REPOS" ]; then
  MANAGED_JSON=$(echo "$MANAGED_REPOS" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | awk 'NF{printf "%s\"%s\"", (NR>1?",":""), $0}' | sed 's/^/[/;s/$/]/')
fi

cat > "$EGREGORE_DIR/egregore.json" <<EOF
{
  "org_name": "${ORG_NAME}",
  "github_org": "${GITHUB_ORG}",
  "memory_repo": "${MEMORY_URL}",
  "api_url": "${API_URL}",
  "repo_name": "${REPO_NAME}",
  "slug": "${ORG_SLUG}",
  "repos": ${MANAGED_JSON}
}
EOF

# ─── Fetch user's Anthropic key from API ─────────────────────────

ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
if [ -z "$ANTHROPIC_KEY" ] && [ -n "${EGREGORE_API_KEY:-}" ] && [ -n "${API_URL:-}" ]; then
  # Get GitHub username for the key lookup
  GH_USERNAME=""
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    GH_USERNAME=$(curl -sf -H "Authorization: token $GITHUB_TOKEN" \
      https://api.github.com/user --max-time 5 2>/dev/null | jq -r '.login // empty')
  fi

  if [ -n "$GH_USERNAME" ]; then
    FETCHED=$(curl -sf "${API_URL}/api/user/keys/fetch?key_name=anthropic_api_key&github_username=${GH_USERNAME}" \
      -H "Authorization: Bearer $EGREGORE_API_KEY" --max-time 5 2>/dev/null || echo '{}')
    KEY_VAL=$(echo "$FETCHED" | jq -r '.value // empty')
    if [ -n "$KEY_VAL" ] && [ "$KEY_VAL" != "null" ]; then
      ANTHROPIC_KEY="$KEY_VAL"
      echo "Fetched Anthropic API key from settings"
    fi
  fi
fi

# ─── Write .env (secrets) ────────────────────────────────────────

cat > "$EGREGORE_DIR/.env" <<EOF
GITHUB_TOKEN=${GITHUB_TOKEN:-}
EGREGORE_API_KEY=${EGREGORE_API_KEY:-}
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
EOF
chmod 600 "$EGREGORE_DIR/.env"

# ─── Ensure ~/.egregore exists ────────────────────────────────────

mkdir -p "$HOME_DIR/.egregore"

echo "Egregore workspace ready: ${ORG_NAME} (${ORG_SLUG})"
