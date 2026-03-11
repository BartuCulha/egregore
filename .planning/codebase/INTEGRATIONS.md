# External Integrations

**Analysis Date:** 2026-03-05

## APIs & External Services

**AI/ML:**
- Anthropic Claude API - Agent decision-making and natural language processing
  - SDK/Client: Direct HTTP calls via httpx (`telegram-bot/bot.py`, `telegram-bot/test_bot.py`)
  - Models: claude-haiku-4-5-20251001, claude-sonnet-4-20250514, claude-opus-4-5-20251101
  - Auth: `ANTHROPIC_API_KEY` environment variable (referenced in `telegram-bot/bot.py:19`)
  - Endpoint: `https://api.anthropic.com/v1/messages`
  - Headers: `anthropic-version: 2023-06-01`

**Messaging:**
- Telegram Bot API - Async notifications and team communication
  - SDK/Client: python-telegram-bot 20.7 with webhooks (`telegram-bot/requirements.txt`)
  - Auth: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (referenced in `api/auth.py`)
  - Implementation: `bin/notify.sh` routes through API gateway, never direct client access
  - Bot management: `api/services/notify.py` handles send/group/test operations

**Email:**
- Resend - Email service (package installed in `api/requirements.txt`)
  - SDK/Client: resend Python package
  - Auth: Environment variable (not specified in available files)
  - Implementation: Imported in API but usage not visible in sampled code

## Data Storage

**Databases:**
- Neo4j AuraDB - Knowledge graph for sessions, people, artifacts, quests
  - Connection: Two separate instances per `DEV.md:12-16`:
    - `NEO4J_HOST`, `NEO4J_USER`, `NEO4J_PASSWORD` - Curve Labs private (AuraDB Free, ID c02bbdac)
    - `EGREGORE_NEO4J_HOST`, `EGREGORE_NEO4J_USER`, `EGREGORE_NEO4J_PASSWORD` - Customer database (AuraDB Business Critical, ID 668bb747)
  - Client: neo4j 5.27.0 (telegram bot), 5.0.0+ (tests)
  - Access: All queries routed through `bin/graph.sh` → API gateway (`/api/graph/query`)
  - Scoping: Tenant isolation via `inject_org_scope()` in `api/services/graph.py`
  - Schema: Person, Session, Artifact, Quest, Project, Spirit, Interview nodes (referenced in `CLAUDE.md`)

- Supabase - Alternative storage backend (optional)
  - Connection: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (referenced in `api/services/supabase.py:26-27`)
  - Client: supabase 2.10.0
  - Feature flag: `USE_SUPABASE` environment variable (referenced in `api/auth.py:14`)
  - Purpose: Replaces Neo4j for org configs when enabled

**File Storage:**
- Git repositories - Shared memory and transcripts
  - Primary memory repo: Defined per org in `egregore.json` → `memory_repo` (e.g., `https://github.com/Curve-Labs/curve-labs-memory.git`)
  - Managed repos: Additional repos listed in `egregore.json` → `repos[]` (e.g., `["lace", "egregore-site"]`)
  - Access: HTTPS with credential storage via `bin/github-auth.sh`

- Local filesystem - Telemetry buffer, session state
  - `~/.egregore/telemetry.jsonl` - Event buffer (flushed at session end)
  - `~/.egregore/instances.json` - Multi-instance registry
  - `.egregore-state.json` - Per-instance onboarding and user state
  - `.egregore-session-id` - Current session identifier

**Caching:**
- None detected

## Authentication & Identity

**Auth Provider:**
- GitHub OAuth - User authentication and org management
  - Implementation: OAuth flow via `api/auth.py:25` (`exchange_github_code`, `GITHUB_CLIENT_ID`)
  - Token storage: `GITHUB_TOKEN` in `.env` (gitignored)
  - Purpose: User identity, org/repo access, invite management
  - GitHub CLI: `gh` command used for PR creation (`bin/` scripts reference)

**Custom:**
- Egregore API Keys - Org-level authentication
  - Generation: `generate_api_key()` in `api/auth.py:23`
  - Storage: `EGREGORE_API_KEY` in `.env`, org config in `ORG_CONFIGS` dict
  - Validation: `validate_api_key()` in `api/auth.py:22`
  - Admin tokens: `validate_admin_github_token()` for privileged operations

## Monitoring & Observability

**Error Tracking:**
- None detected (Python logging only via `logging.basicConfig()`)

**Logs:**
- Python: Standard library logging (`logging.basicConfig(level=logging.INFO)` in `api/main.py:40`, `telegram-bot/bot.py`)
- Bash: stderr output from scripts

**Telemetry:**
- Custom telemetry system via `bin/telemetry.sh`
  - Endpoint: Routes through Egregore API gateway (`api_url` from `egregore.json`)
  - Auth: `EGREGORE_API_KEY` from `.env`
  - Events: Command usage, session durations, error codes (never code/content)
  - Storage: Local buffer at `~/.egregore/telemetry.jsonl`, flushed via `bin/transcript-archive.sh`
  - Opt-out: `EGREGORE_NO_TELEMETRY=1` or `DO_NOT_TRACK=1`

## CI/CD & Deployment

**Hosting:**
- Railway - API gateway and telegram bot deployment
  - Project: Referenced in `telegram-bot/railway.json`
  - API URL: `https://egregore-production-55f2.up.railway.app` (from `egregore.json:5`)
  - Build: Dockerfile-based (`Dockerfile` in project root)
  - Restart policy: ON_FAILURE, max 5 retries

- Netlify - Static site hosting
  - Config: `netlify.toml`
  - Build: `npm run build` in `site/` directory
  - Publish: `site/dist`
  - Node version: 20

- GitHub Container Registry - Docker image storage
  - Registry: ghcr.io
  - Image: `{github.repository_owner}/egregore-workspace`
  - Trigger: Push to main/develop, or workflow_dispatch

**CI Pipeline:**
- GitHub Actions - Workspace image build
  - Workflow: `.github/workflows/build-image.yml`
  - Triggers: Push to main/develop (affecting docker/, bin/, .claude/, CLAUDE.md), manual dispatch
  - Permissions: contents:read, packages:write
  - Actions: checkout@v4, docker/login-action@v3, docker/metadata-action@v5, docker/build-push-action@v5

## Environment Configuration

**Required env vars:**
- `GITHUB_TOKEN` - GitHub authentication (auto-created during onboarding)
- `EGREGORE_API_KEY` - Org's API key (provided during setup)

**Server-side only (never in client .env):**
- `NEO4J_HOST`, `NEO4J_USER`, `NEO4J_PASSWORD` - CL private database
- `EGREGORE_NEO4J_HOST`, `EGREGORE_NEO4J_USER`, `EGREGORE_NEO4J_PASSWORD` - Customer database
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - Telegram credentials
- `ANTHROPIC_API_KEY` - Claude API access (bot-side only)
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` - Optional storage backend
- `CORS_ORIGINS` - API CORS whitelist (defaults to egregore.xyz domains)

**Optional:**
- `EGREGORE_NO_TELEMETRY=1` - Disable telemetry
- `DO_NOT_TRACK=1` - Standard opt-out flag
- `USE_SUPABASE=true` - Enable Supabase backend

**Secrets location:**
- Client: `.env` in project root (gitignored, created during onboarding)
- Server: Railway environment variables (set via dashboard)
- Template: `.env.example` shows required client variables

## Webhooks & Callbacks

**Incoming:**
- GitHub OAuth callback - `api/auth.py` (`exchange_github_code`, `GitHubCallback` model)
  - Purpose: Complete OAuth flow after user authorizes
- Telegram webhooks - `telegram-bot/bot.py` (starlette routes + python-telegram-bot integration)
  - Purpose: Receive bot commands and messages

**Outgoing:**
- None detected (all integrations are request-based)

---

*Integration audit: 2026-03-05*
