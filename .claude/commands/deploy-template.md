Push the Coder workspace template to a hosted Egregore VPS.

Arguments: $ARGUMENTS (optional: org slug, defaults to "curvelabs")

## When to invoke

- "deploy template", "push template", "update template"
- After changing `docker/egregore-template/main.tf`
- After changing startup_script or template parameters

## What to do

### 1. Resolve org

Default slug is `curvelabs`. If $ARGUMENTS contains a slug, use that instead.

Look up hosting info from Supabase (or use known values for curvelabs):

For **curvelabs**:
- VPS IP: from `orgs.hosting_ip` (currently 188.34.204.91)
- Coder password: from `orgs.hosting_coder_password`

If you don't have the password, ask the user.

### 2. Get Coder session token

```bash
CODER_TOKEN=$(curl -sf http://$VPS_IP/api/v2/users/login \
  -d '{"email":"admin@egregore.xyz","password":"'$CODER_PWD'"}' | jq -r '.session_token')
```

### 3. Copy template to VPS

```bash
sshpass -p "$SSH_PWD" scp -o StrictHostKeyChecking=no \
  docker/egregore-template/main.tf \
  root@$VPS_IP:/tmp/egregore-template/main.tf
```

### 4. Push via Coder CLI

```bash
sshpass -p "$SSH_PWD" ssh -o StrictHostKeyChecking=no root@$VPS_IP "
  export CODER_URL=http://localhost
  coder login --token $CODER_TOKEN http://localhost 2>/dev/null || true

  coder templates push Egregore \
    --directory /tmp/egregore-template \
    --name $VERSION_NAME \
    --var 'egregore_api_key=$API_KEY' \
    --var 'api_url=https://egregore-production-55f2.up.railway.app' \
    --var 'memory_url=$MEMORY_URL' \
    --var 'fork_url=$FORK_URL' \
    --var 'ghcr_token=not-needed' \
    --var 'github_token=$GITHUB_TOKEN' \
    --yes
"
```

Get the variable values from:
- `egregore_api_key`: Supabase `orgs` table or ask user
- `memory_url`, `fork_url`: derive from `github_org` and `repo_name` in Supabase
- `github_token`: from VPS `/opt/egregore/github-token` or ask user

Use a descriptive `--name` based on what changed (e.g., `fix-bootstrap-v7`, `add-managed-repos-v2`).

### 5. Optionally restart a workspace

If the user wants to test, ask which workspace to restart:

```bash
# Stop
curl -s "$CODER_URL/api/v2/workspaces/$WS_ID/builds" \
  -H "Coder-Session-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"transition":"stop"}'

# Wait for stopped, then start with new version
ACTIVE=$(curl -sf "$CODER_URL/api/v2/templates/$TEMPLATE_ID" \
  -H "Coder-Session-Token: $TOKEN" | jq -r '.active_version_id')

curl -s "$CODER_URL/api/v2/workspaces/$WS_ID/builds" \
  -H "Coder-Session-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"transition\":\"start\",\"template_version_id\":\"$ACTIVE\"}"
```

### 6. Output

```
Template pushed to Coder (188.34.204.91)
  Version: fix-bootstrap-v7
  Template: Egregore (arm64)

  To apply: restart workspace from Coder UI or run /deploy-template restart
```

## Rules

- **Never hardcode passwords in the command file.** Read from Supabase or ask user.
- **Always use `--yes`** to skip confirmation prompts on the VPS.
- **`sshpass` is required** — check with `which sshpass` first.
- **Template push is CLI-only** — the Coder REST API for template versions is buggy (panics on workspace tags in v2.31).
