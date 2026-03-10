terraform {
  required_providers {
    coder = {
      source = "coder/coder"
    }
    docker = {
      source = "kreuzwerker/docker"
    }
  }
}

provider "coder" {}
provider "docker" {
  registry_auth {
    address  = "ghcr.io"
    username = "oauth2"
    password = var.ghcr_token
  }
}

# ─── Template parameters (set per org by admin) ──────────────────

data "coder_parameter" "org_slug" {
  name         = "org_slug"
  display_name = "Org Slug"
  description  = "The Egregore org identifier"
  type         = "string"
  mutable      = false
}

data "coder_parameter" "org_name" {
  name         = "org_name"
  display_name = "Org Name"
  description  = "Display name for the org"
  type         = "string"
  mutable      = false
}

data "coder_parameter" "github_org" {
  name         = "github_org"
  display_name = "GitHub Org"
  description  = "GitHub org or user that owns the repos"
  type         = "string"
  mutable      = false
}

data "coder_parameter" "repo_name" {
  name         = "repo_name"
  display_name = "Repo Name"
  description  = "Name of the Egregore repo (fork)"
  type         = "string"
  default      = "egregore-core"
  mutable      = false
}

data "coder_parameter" "managed_repos" {
  name         = "managed_repos"
  display_name = "Managed Repos"
  description  = "Comma-separated list of managed repos"
  type         = "string"
  default      = ""
  mutable      = false
}

# ─── Workspace metadata ──────────────────────────────────────────

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# ─── External auth: user's GitHub token for private repo access ──

data "coder_external_auth" "github" {
  id = "github"
}

# ─── Org-level secrets (set by admin in Coder template variables) ─

variable "egregore_api_key" {
  type      = string
  sensitive = true
}

variable "api_url" {
  type    = string
  default = "https://egregore-production-55f2.up.railway.app"
}

variable "memory_url" {
  type = string
}

variable "fork_url" {
  type = string
}

variable "ghcr_token" {
  type      = string
  sensitive = true
}

variable "github_token" {
  type        = string
  sensitive   = true
  description = "Org-level GitHub token for API operations (graph, notifications)"
}

# ─── Docker image ────────────────────────────────────────────────

resource "docker_image" "egregore" {
  name         = "ghcr.io/curve-labs/egregore-workspace:latest"
  keep_locally = true
}

# ─── Container ───────────────────────────────────────────────────

resource "docker_container" "workspace" {
  count = data.coder_workspace.me.start_count
  name  = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}"
  image = docker_image.egregore.image_id

  env = [
    "CODER_AGENT_TOKEN=${coder_agent.main.token}",
    # Org-level tokens for API operations (NOT for git cloning — that uses external auth)
    "GITHUB_TOKEN=${var.github_token}",
    "EGREGORE_API_KEY=${var.egregore_api_key}",
    # User identity
    "CODER_USERNAME=${data.coder_workspace_owner.me.name}",
    # Org config (used by Python init as fallback if API unreachable)
    "ORG_SLUG=${data.coder_parameter.org_slug.value}",
    "ORG_NAME=${data.coder_parameter.org_name.value}",
    "GITHUB_ORG=${data.coder_parameter.github_org.value}",
    "REPO_NAME=${data.coder_parameter.repo_name.value}",
    "MANAGED_REPOS=${data.coder_parameter.managed_repos.value}",
    "API_URL=${var.api_url}",
    "MEMORY_URL=${var.memory_url}",
    "FORK_URL=${var.fork_url}",
  ]

  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }

  # Persistent home directory across workspace restarts
  volumes {
    volume_name    = "coder-${data.coder_workspace_owner.me.name}-${lower(data.coder_workspace.me.name)}-home"
    container_path = "/home/egregore"
    read_only      = false
  }

  user = "egregore"

  # Fix volume ownership (may differ between image builds) then start Coder agent
  command = ["sh", "-c", "sudo chown -R $(id -u):$(id -g) $HOME 2>/dev/null; ${replace(coder_agent.main.init_script, "/localhost|127\\.0\\.0\\.1/", "host.docker.internal")}"]
}

# ─── Coder agent ─────────────────────────────────────────────────

resource "coder_agent" "main" {
  os   = "linux"
  arch = "arm64"
  dir  = "/home/egregore/egregore"

  display_apps {
    vscode       = false
    web_terminal = false
    ssh_helper   = true
  }

  startup_script = <<-EOT
    # Install Claude Code if missing (volume mount hides image's ~/.local/bin)
    if ! command -v claude &>/dev/null; then
      curl -fsSL https://claude.ai/install.sh | bash
    fi

    # ── Git setup ─────────────────────────────────────────────────────
    EGREGORE_DIR="$HOME/egregore"
    MEMORY_DIR="$HOME/memory"

    # Git credentials (org-level token for cloning private repos)
    if [ -n "$GITHUB_TOKEN" ]; then
      git config --global credential.helper store
      echo "https://x-access-token:$${GITHUB_TOKEN}@github.com" > "$HOME/.git-credentials"
      chmod 600 "$HOME/.git-credentials"
    fi

    # Git identity
    if [ -n "$CODER_USERNAME" ] && [ -z "$(git config --global user.name 2>/dev/null)" ]; then
      git config --global user.name "$CODER_USERNAME"
      git config --global user.email "$CODER_USERNAME@users.noreply.github.com"
    fi

    # ── Clone repos ───────────────────────────────────────────────────
    if [ ! -d "$EGREGORE_DIR/.git" ]; then
      git clone "${var.fork_url}" "$EGREGORE_DIR" 2>&1 || echo "[init] Warning: could not clone egregore repo"
    else
      cd "$EGREGORE_DIR" && git fetch origin --quiet 2>/dev/null || true
      BRANCH=$(git branch --show-current 2>/dev/null)
      if [ "$BRANCH" = "develop" ] || [ "$BRANCH" = "main" ]; then
        git pull --ff-only origin "$BRANCH" 2>/dev/null || true
      fi
    fi

    if [ -n "${var.memory_url}" ] && [ ! -d "$MEMORY_DIR/.git" ]; then
      git clone "${var.memory_url}" "$MEMORY_DIR" 2>&1 || echo "[init] Warning: could not clone memory repo"
    elif [ -d "$MEMORY_DIR/.git" ]; then
      cd "$MEMORY_DIR" && git pull --ff-only origin main 2>/dev/null || true
    fi

    # Link memory if cloned
    if [ -d "$MEMORY_DIR/.git" ] && [ -d "$EGREGORE_DIR" ]; then
      ln -sfn "$MEMORY_DIR" "$EGREGORE_DIR/memory"
    fi

    # ── Config from API (writes into cloned repo dir) ─────────────────
    python3 /opt/egregore/bin/workspace-init.py

    # ── Shell setup ───────────────────────────────────────────────────
    cat > "$HOME/.zshrc" <<'ZSHRC'
export PATH="/home/egregore/.local/bin:/home/egregore/.claude/bin:/opt/egregore/bin:$PATH"
egregore() { cd ~/egregore && claude "start"; }

# Auto-start Egregore on first interactive terminal (not subshells)
if [[ -o interactive ]] && [[ ! -f /tmp/.egregore-started ]] && [[ -d "$HOME/egregore" ]] && command -v claude &>/dev/null; then
  touch /tmp/.egregore-started
  cd ~/egregore
  claude "start"
fi
ZSHRC

    cat > "$HOME/.egregore-bootstrap.sh" <<'BOOT'
#!/bin/bash
export PATH="$HOME/.local/bin:$HOME/.claude/bin:/usr/local/bin:$PATH"
if [ -d "$HOME/egregore" ] && command -v claude &>/dev/null; then
  cd ~/egregore
  exec claude "start"
else
  echo "  Workspace setup incomplete. Type 'egregore' to retry."
  exec zsh
fi
BOOT
    chmod +x "$HOME/.egregore-bootstrap.sh"
  EOT

  startup_script_behavior = "blocking"

  metadata {
    key          = "org"
    display_name = "Egregore"
    script       = "jq -r '.org_name' ~/egregore/egregore.json 2>/dev/null || echo 'initializing...'"
    interval     = 30
  }

  metadata {
    key          = "branch"
    display_name = "Branch"
    script       = "cd ~/egregore && git branch --show-current 2>/dev/null || echo '-'"
    interval     = 10
  }
}

# ─── Web terminal app (auto-starts Egregore) ─────────────────────

resource "coder_app" "terminal" {
  agent_id     = coder_agent.main.id
  slug         = "terminal"
  display_name = "Terminal"
  icon         = "/icon/terminal.svg"
  command      = "/home/egregore/.egregore-bootstrap.sh"
}
