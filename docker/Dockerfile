# Egregore Base Image
# Pre-installs all dependencies so Coder workspaces start fast.
# Published to ghcr.io/curve-labs/egregore-workspace

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Core dependencies (matches what session-start.sh expects)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    jq \
    curl \
    ca-certificates \
    gnupg \
    zsh \
    sudo \
    openssh-client \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (gh)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS (needed for npx create-egregore, Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for workspaces
RUN useradd -m -s /bin/zsh -G sudo egregore \
    && echo "egregore ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/egregore

# Copy Egregore framework files (bin/, .claude/, CLAUDE.md)
# These are the shared framework — workspace-specific config is injected at startup
COPY bin/ /opt/egregore/bin/
COPY .claude/ /opt/egregore/.claude/
COPY CLAUDE.md /opt/egregore/CLAUDE.md
COPY docker/egregore.json.template /opt/egregore/egregore.json.template

# Make bin scripts executable
RUN chmod +x /opt/egregore/bin/*.sh /opt/egregore/bin/*.py

USER egregore
WORKDIR /home/egregore

# Claude Code CLI (install as egregore user so it goes to ~/.local/bin)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Set up zsh with minimal config
RUN echo 'export PATH="/home/egregore/.local/bin:/opt/egregore/bin:$PATH"' >> ~/.zshrc

CMD ["zsh"]
