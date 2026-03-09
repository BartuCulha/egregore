Build and deploy the Docker workspace image to a hosted Egregore VPS.

Arguments: $ARGUMENTS (optional: "status" to just check build, org slug defaults to "curvelabs")

## When to invoke

- "deploy image", "rebuild image", "update image"
- After changing `docker/Dockerfile`, `bin/workspace-init.sh`, `.claude/`, or `CLAUDE.md`
- After merging to develop/main (build triggers automatically)

## What to do

### 1. Check if build is needed

The GitHub Actions workflow `build-image.yml` triggers on pushes to main/develop that change `docker/`, `bin/`, `.claude/`, or `CLAUDE.md`.

```bash
# Check recent builds
gh run list --repo Curve-Labs/egregore --workflow build-image.yml --limit 3
```

If $ARGUMENTS is "status", just show the build status and stop.

If no recent build or the latest failed:
```bash
# Trigger manually
gh workflow run build-image.yml --repo Curve-Labs/egregore --ref develop
```

### 2. Wait for build

The multi-arch build takes ~9 minutes.

```bash
# Get the run ID and watch
RUN_ID=$(gh run list --repo Curve-Labs/egregore --workflow build-image.yml --limit 1 --json databaseId -q '.[0].databaseId')
gh run watch $RUN_ID --repo Curve-Labs/egregore
```

### 3. Pull image on VPS

The GHCR package is private. Need a token with `write:packages` scope.

```bash
GH_TOKEN=$(gh auth token)

sshpass -p "$SSH_PWD" ssh -o StrictHostKeyChecking=no root@$VPS_IP "
  echo '$GH_TOKEN' | docker login ghcr.io -u oguzhan --password-stdin
  docker pull ghcr.io/curve-labs/egregore-workspace:latest
  docker images ghcr.io/curve-labs/egregore-workspace:latest --format '{{.Architecture}} {{.Size}}'
"
```

If the pull fails with "denied":
1. Check `gh auth status` — needs `write:packages` or `read:packages` scope
2. If missing: `gh auth refresh -h github.com -s read:packages,write:packages`
3. Retry with new token

### 4. Restart workspaces

Existing workspaces need to be stopped and started to pick up the new image.

```bash
# List running workspaces
sshpass -p "$SSH_PWD" ssh root@$VPS_IP "docker ps --format '{{.Names}} {{.Status}}'"
```

Ask the user which workspaces to restart (or all). For each:
- Stop via Coder API
- Wait for stopped
- Start with latest template version

### 5. Output

```
Docker image deployed to 188.34.204.91
  Image: ghcr.io/curve-labs/egregore-workspace:latest
  Arch: arm64
  Size: 623MB
  Build: #22864524481 (9m12s)

  Running workspaces: oz, ali, cem, renc
  Restart needed to pick up new image.
```

## Rules

- **Never `docker rmi` the old image while containers are running** — it will fail or break running workspaces.
- **GHCR is private** — always authenticate before pulling. The `gh auth token` must have packages scope.
- **Multi-arch build produces both amd64+arm64** — Docker on the ARM VPS auto-selects arm64.
- **`sshpass` is required** for SSH to VPS.
- **Don't force-restart all workspaces without asking** — users may have active sessions.
