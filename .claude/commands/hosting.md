Manage hosted Egregore workspaces — enable, check status, create user workspaces.

Arguments: $ARGUMENTS (subcommand + optional org slug)

## When to invoke

- "enable hosting", "set up VPS", "hosted egregore", "provision workspace"
- "hosting status", "check VPS", "is hosting ready"
- "create workspace for [user]", "add hosted user"

## Usage

- `/hosting enable [slug]` — Provision VPS + Coder for an org
- `/hosting status [slug]` — Check VPS + Coder readiness
- `/hosting users [slug]` — Create workspaces for all members who don't have one
- `/hosting user [slug] [github_username]` — Create workspace for one member
- `/hosting list` — Show all orgs with hosting info

## Resolve org

Default slug: read from `egregore.json` → `slug` field.
If $ARGUMENTS contains a slug, use that instead.

Get API URL and GitHub token:
```bash
API_URL=$(jq -r '.api_url' egregore.json)
GITHUB_TOKEN=$(grep '^GITHUB_TOKEN=' .env | cut -d'=' -f2-)
```

---

## Route: enable

Provisions a Hetzner VPS, installs Coder, deploys workspace template.

### Step 1: Check if already enabled

```bash
curl -s "$API_URL/api/hosting/status/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN"
```

If returns hosting info (IP, coder_ready, etc.) → already enabled, show status instead.
If returns `{"status": "not_found"}` → proceed.

### Step 2: Enable hosting

```bash
curl -s -X POST "$API_URL/api/hosting/enable/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN"
```

Expected response: `{"status": "provisioning", "ip": "...", "slug": "..."}`

If error, show it and stop.

### Step 3: Poll for readiness

VPS + Coder takes 3-5 minutes. Poll every 30 seconds:

```bash
curl -s "$API_URL/api/hosting/status/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN"
```

Show progress:
```
  Provisioning VPS for {slug}...
  IP: {ip}
  ⏳ Waiting for Coder to be ready...
  ⏳ cloud-init running... (check 2/10)
  ✓ Coder ready at http://{ip}
  ✓ Session token stored
```

Stop polling after `coder_ready: true` or after 10 attempts (5 min).

If `coder_ready: true` but no session token stored, the status endpoint auto-stores it.

### Step 4: Get VPS SSH access

The root password is the `coder_password` generated during provisioning, stored in Supabase.

```bash
# Get password from Supabase (need SUPABASE_URL + SUPABASE_SERVICE_KEY from api/.env)
SUPABASE_URL=$(grep '^SUPABASE_URL=' api/.env | cut -d'=' -f2-)
SUPABASE_KEY=$(grep '^SUPABASE_SERVICE_KEY=' api/.env | cut -d'=' -f2-)
VPS_PWD=$(curl -s "$SUPABASE_URL/rest/v1/orgs?slug=eq.$SLUG&select=hosting_coder_password" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" | \
  python3 -c "import sys,json; print(json.load(sys.stdin)[0]['hosting_coder_password'])")
```

All subsequent SSH commands use: `sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP`

Verify `sshpass` is installed locally: `which sshpass` (install via `brew install hudochenkov/sshpass/sshpass` if missing).

### Step 5: Create per-org GitHub OAuth App

**CRITICAL**: Each Coder VPS needs its own GitHub OAuth app — callback URLs are IP-specific. A shared OAuth app will fail with "redirect_uri is not associated with this application."

Tell the user:

> Coder needs a GitHub OAuth app for login. Create one now:
>
> 1. Go to https://github.com/organizations/{github_org}/settings/applications/new
>    (or https://github.com/settings/applications/new for personal)
> 2. **Application name**: `Egregore Coder — {slug}`
> 3. **Homepage URL**: `http://{ip}`
> 4. **Authorization callback URL**: `http://{ip}/api/v2/users/oauth2/github/callback`
> 5. Click "Register application"
> 6. Copy the **Client ID**
> 7. Click "Generate a new client secret" and copy the **Client Secret**
>
> Give me the Client ID and Client Secret.

**Important**: GitHub OAuth apps only allow ONE callback URL. Cannot reuse an existing app from another VPS.

Once user provides credentials, SSH into the VPS and configure BOTH OAuth login AND external auth (for repo cloning):

```bash
sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "
# Update OAuth login credentials
sed -i 's/^CODER_OAUTH2_GITHUB_CLIENT_ID=.*/CODER_OAUTH2_GITHUB_CLIENT_ID=$CLIENT_ID/' /etc/coder.d/coder.env
sed -i 's/^CODER_OAUTH2_GITHUB_CLIENT_SECRET=.*/CODER_OAUTH2_GITHUB_CLIENT_SECRET=$CLIENT_SECRET/' /etc/coder.d/coder.env

# Add external auth (required for workspaces to clone private repos)
# Check if already present to avoid duplicates
if ! grep -q 'CODER_EXTERNAL_AUTH_0_TYPE' /etc/coder.d/coder.env; then
  cat >> /etc/coder.d/coder.env <<EOF
CODER_EXTERNAL_AUTH_0_TYPE=github
CODER_EXTERNAL_AUTH_0_ID=github
CODER_EXTERNAL_AUTH_0_CLIENT_ID=$CLIENT_ID
CODER_EXTERNAL_AUTH_0_CLIENT_SECRET=$CLIENT_SECRET
EOF
else
  sed -i 's/^CODER_EXTERNAL_AUTH_0_CLIENT_ID=.*/CODER_EXTERNAL_AUTH_0_CLIENT_ID=$CLIENT_ID/' /etc/coder.d/coder.env
  sed -i 's/^CODER_EXTERNAL_AUTH_0_CLIENT_SECRET=.*/CODER_EXTERNAL_AUTH_0_CLIENT_SECRET=$CLIENT_SECRET/' /etc/coder.d/coder.env
fi

systemctl restart coder
"
```

Wait for Coder to come back up:
```bash
for i in $(seq 1 10); do
  curl -sf "http://$IP/api/v2/buildinfo" > /dev/null 2>&1 && break
  sleep 2
done
```

Confirm:
```
  ✓ GitHub OAuth configured for {slug}
  ✓ External auth configured (workspace repo cloning)
  ✓ Coder restarted
  Login URL: http://{ip} (GitHub OAuth)
```

### Step 6: Verify GHCR image is pullable

The workspace image `ghcr.io/curve-labs/egregore-workspace:latest` must be publicly accessible. Test:

```bash
sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "
docker logout ghcr.io 2>/dev/null
docker pull ghcr.io/curve-labs/egregore-workspace:latest
"
```

If pull fails with `denied`:
- The GHCR package is private. Go to: https://github.com/orgs/{github_org}/packages/container/egregore-workspace/settings
- Change visibility to **Public**
- The org may need to allow public packages first: https://github.com/organizations/{github_org}/settings/packages
- Retry the pull

### Step 7: Deploy workspace template

Cloud-init installs Coder but does NOT push the workspace template. You must deploy it.

```bash
# Copy template to VPS
sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "mkdir -p /tmp/egregore-template"
sshpass -p "$VPS_PWD" scp -o StrictHostKeyChecking=no \
  docker/egregore-template/main.tf root@$IP:/tmp/egregore-template/main.tf

# Get Coder session token
CODER_TOKEN=$(curl -sf "http://$IP/api/v2/users/login" \
  -d '{"email":"admin@egregore.xyz","password":"'"$VPS_PWD"'"}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_token',''))")

# Read org config from VPS
ORG_CFG=$(sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "cat /opt/egregore/org-config.json")
API_KEY=$(echo "$ORG_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin)['egregore_api_key'])")
FORK_URL=$(echo "$ORG_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin)['fork_url'])")
MEMORY_URL=$(echo "$ORG_CFG" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_url'])")
GH_TOKEN_VPS=$(sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "cat /opt/egregore/github-token 2>/dev/null")

# Push template
sshpass -p "$VPS_PWD" ssh -o StrictHostKeyChecking=no root@$IP "
  export CODER_URL=http://localhost
  coder login --token $CODER_TOKEN http://localhost 2>/dev/null || true
  coder templates push Egregore \
    --directory /tmp/egregore-template \
    --name ${SLUG}-initial \
    --var 'egregore_api_key=$API_KEY' \
    --var 'api_url=$API_URL' \
    --var 'memory_url=$MEMORY_URL' \
    --var 'fork_url=$FORK_URL' \
    --var 'ghcr_token=not-needed' \
    --var 'github_token=$GH_TOKEN_VPS' \
    --yes
"
```

If template push fails with `external auth provider "github" is not configured` — Step 5 was incomplete. Verify `/etc/coder.d/coder.env` has the `CODER_EXTERNAL_AUTH_0_*` lines and Coder was restarted.

Confirm:
```
  ✓ Template "Egregore" deployed
```

### Step 8: Offer to create workspaces

After template is deployed, ask:

> VPS ready with template. Create workspaces for all {N} active members now?

If yes, run the `users` route.

---

## Route: status

```bash
RESULT=$(curl -s "$API_URL/api/hosting/status/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN")
```

Display:
```
  Hosting: {slug}
  IP: {ip}
  Coder: {coder_url}
  VPS: {vps_status}
  Coder ready: {coder_ready}
  Token stored: {token_stored}
```

---

## Route: users

Create Coder users + workspaces for all active members who don't have one yet.

### Step 1: Get members

```bash
curl -s "$API_URL/api/admin/org/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN"
```

Filter to active members.

### Step 2: For each member, create user + workspace

```bash
# Create Coder user
curl -s -X POST "$API_URL/api/hosting/user/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"github_username": "'$USERNAME'"}'

# Create workspace
curl -s -X POST "$API_URL/api/hosting/workspace/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"github_username": "'$USERNAME'"}'
```

Show progress:
```
  Creating workspaces for {slug}...
  ✓ neno-is-ooo — user + workspace created
  ✓ renckorzay — user + workspace created
  ✗ bmorphism — error: {detail}
  ...
  Done: {N} created, {M} failed
```

### Step 3: Show access info

```
  Coder URL: http://{ip}
  Login: GitHub OAuth
  Members can open their workspace in browser.
```

---

## Route: user (single)

Same as `users` but for one specific github_username.

```bash
curl -s -X POST "$API_URL/api/hosting/user/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"github_username": "'$USERNAME'"}'

curl -s -X POST "$API_URL/api/hosting/workspace/$SLUG" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"github_username": "'$USERNAME'"}'
```

---

## Route: list

```bash
# Get all orgs and their hosting status
# Platform admin only
curl -s "$API_URL/api/admin/health" \
  -H "Authorization: Bearer $GITHUB_TOKEN"
```

Show table of orgs with hosting_enabled status.

---

## Rules

- All API calls use GitHub token auth (not API key)
- VPS provisioning is idempotent — if already enabled, show status
- Never expose coder_password in output
- Poll with 30-second intervals, max 10 attempts
- Create users sequentially (not parallel) to avoid rate limiting
