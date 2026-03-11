# Architecture

**Analysis Date:** 2026-03-05

## Pattern Overview

**Overall:** Hub-and-spoke coordination system with multi-tenant API gateway

**Key Characteristics:**
- Git-based shared memory with Neo4j knowledge graph overlay
- FastAPI backend gateway routing client requests to shared infrastructure
- Claude Code agents as primary interface with hook-driven session lifecycle
- Telegram bot for async notifications and natural language queries
- Multi-org tenant isolation via query-scoped org parameter injection

## Layers

**Client Layer:**
- Purpose: Local egregore instances running in Claude Code sessions
- Location: `bin/` (bash orchestration scripts), `.claude/commands/` (slash commands), `.claude/hooks/` (lifecycle hooks)
- Contains: Session management, git operations, command routing, boundary enforcement
- Depends on: API gateway (`bin/graph.sh`, `bin/notify.sh`), local git repos, `.env` secrets
- Used by: Human users via Claude Code interface

**API Gateway:**
- Purpose: Routes authenticated requests to shared infrastructure (Neo4j, Telegram, GitHub)
- Location: `api/main.py`, `api/services/`
- Contains: Multi-tenant auth, query scoping, rate limiting, org provisioning endpoints
- Depends on: Neo4j (two databases: CL private + customer shared), Telegram Bot API, GitHub OAuth/API
- Used by: Client instances via HTTP (curl from bash scripts)

**Data Layer:**
- Purpose: Persistent knowledge graph and notification channels
- Location: Neo4j AuraDB (two instances), Telegram
- Contains: Person/Session/Artifact/Quest/Handoff nodes with org-scoped properties
- Depends on: API gateway for query execution and guard layer
- Used by: API gateway exclusively (clients never access directly)

**Frontend (Website):**
- Purpose: Web-based setup flow and documentation
- Location: Separate repo (`egregore-site`), deployed to Netlify
- Contains: React/Vite SPA for GitHub OAuth, org setup, invite flows
- Depends on: API gateway for setup/invite endpoints, GitHub OAuth app
- Used by: Founders during initial setup, joiners accepting invites

**Bot Layer:**
- Purpose: Telegram interface for async queries and notifications
- Location: `telegram-bot/bot.py`, `telegram-bot/mcp_server.py`
- Contains: Natural language query parsing (via Claude Haiku), org routing, webhook handlers
- Depends on: Same Neo4j + Telegram credentials as API, shares org registry
- Used by: Team members via Telegram groups/DMs

## Data Flow

**Session Start Flow:**

1. User launches Claude Code in egregore directory
2. `.claude/hooks/boundary-check.sh` runs on first tool use (PreToolUse hook)
3. `bin/session-start.sh` executes automatically (SessionStart hook)
4. Script syncs git repos, creates session branch, loads greeting from memory
5. Claude displays ASCII art greeting + asks "What are you working on?"
6. User's response triggers automatic branch creation (`dev/{author}/{topic-slug}`)
7. Session metadata recorded to Neo4j via `bash bin/graph-op.sh set-topic`

**Knowledge Capture Flow:**

1. User invokes command (e.g., `/handoff`, `/reflect`, `/save`)
2. Command file (`.claude/commands/*.md`) loaded and executed by Claude
3. Data written to `memory/` directory (markdown files)
4. Graph metadata written to Neo4j via `bin/graph.sh query`
5. Notifications sent via `bin/notify.sh send` if recipient specified
6. Git operations push changes to memory repo
7. Telemetry events buffered locally to `~/.egregore/telemetry.jsonl`

**Multi-Org Query Flow:**

1. Client calls `bin/graph.sh query "MATCH (p:Person) RETURN p.name"` with EGREGORE_API_KEY
2. `graph.sh` wraps query in JSON and POSTs to `{API_URL}/api/graph/query`
3. API validates key via `validate_api_key()` → resolves org from ORG_CONFIGS
4. `inject_org_scope()` rewrites query to `MATCH (p:Person {org: $_org}) RETURN p.name`
5. Guard layer validates query (blocks DELETE/DROP, checks rate limits)
6. API executes scoped query against org's Neo4j database (CL private or customer shared)
7. Results returned as JSON, `graph.sh` outputs to stdout
8. Telemetry event emitted (fire-and-forget)

**State Management:**
- Session state: `.egregore-state.json` (local, per-user onboarding progress and preferences)
- Org config: `egregore.json` (committed, non-secret: org name, GitHub org, memory repo URL, API URL)
- Secrets: `.env` (gitignored: GITHUB_TOKEN, EGREGORE_API_KEY)
- In-memory: API's `ORG_CONFIGS` dict loaded from Neo4j Org nodes at startup

## Key Abstractions

**Session:**
- Purpose: Unit of work by one person on one topic
- Examples: Tracked via Neo4j Session nodes, linked to Person via BY relationship
- Pattern: Created on launch, topic set on first working branch, artifacts linked via RELATES_TO

**Handoff:**
- Purpose: Async work coordination with status tracking
- Examples: `memory/handoffs/{date}-{slug}.md` files, Session→HANDED_TO→Person edges
- Pattern: Created via `/handoff`, triaged on next bare `/handoff`, status updated in Neo4j

**Org:**
- Purpose: Tenant boundary for multi-org isolation
- Examples: Neo4j Org nodes with `{id, name, api_key, github_org, neo4j_host}`
- Pattern: Created during setup, loaded into `ORG_CONFIGS` at API startup, injected as `{org: $_org}` in all queries

**Command:**
- Purpose: Reusable Claude behavior triggered by slash or intent
- Examples: `.claude/commands/activity.md`, `.claude/commands/save.md`
- Pattern: Markdown spec with "When to invoke" section, executed by Claude's command awareness

**Boundary:**
- Purpose: Session isolation for multi-instance safety
- Examples: PreToolUse hook validates all Read/Write/Bash paths against `bin/boundary.sh`
- Pattern: Allowed paths include project dir, memory symlink, managed repos from `egregore.json`, `~/.claude`, system paths

## Entry Points

**Session Launch:**
- Location: `.claude/hooks/session-start.sh` invoked before first user message
- Triggers: Claude Code startup in egregore directory
- Responsibilities: Git sync (develop + memory), branch status check, load instances registry, emit greeting, set framework version

**Command Invocation:**
- Location: User types `/command` or Claude detects intent from natural language
- Triggers: User message matches command's "When to invoke" phrases
- Responsibilities: Load `.claude/commands/{command}.md` spec, execute steps, emit telemetry event

**API Request:**
- Location: `api/main.py` FastAPI app routes
- Triggers: HTTP POST from client scripts (`bin/graph.sh`, `bin/notify.sh`) or web frontend
- Responsibilities: Validate API key, load org config, route to service layer, return JSON

**Telegram Webhook:**
- Location: `telegram-bot/bot.py` Starlette app at `/webhook`
- Triggers: Telegram sends update (message, command, chat join)
- Responsibilities: Route by chat ID to org config, parse with Haiku, query Neo4j, format response

**Setup Flow:**
- Location: `POST /api/org/setup` endpoint
- Triggers: Web frontend after GitHub OAuth
- Responsibilities: Fork egregore-core, create memory repo, write Org node to Neo4j, generate API key, return setup token

## Error Handling

**Strategy:** Graceful degradation with local fallbacks

**Patterns:**
- API queries return `{"error": "..."}` dicts, not exceptions (check via `jq -e '.error'`)
- Graph offline → commands fall back to filesystem operations (e.g., `/activity` reads `memory/handoffs/index.md` directly)
- Missing config → onboarding flow triggers (`"onboarding_complete": false` in state file)
- Boundary violations → PreToolUse hook rejects with clear message before tool executes
- Rate limits → Guard layer returns 429 with `rate_limited: true` flag

## Cross-Cutting Concerns

**Logging:** Python `logging` module at INFO level, Railway captures stdout/stderr

**Validation:**
- Query guard layer blocks destructive operations (DELETE, DROP, REMOVE)
- Parameter guard blocks `_org` and `org` parameters (reserved for injection)
- Path validation via `bin/boundary.sh` enforces session isolation
- Pydantic models validate API request bodies

**Authentication:**
- Client→API: Bearer token (EGREGORE_API_KEY from `.env`)
- Web→API: GitHub OAuth code exchange → session token
- Telegram→API: Shared bot token (TELEGRAM_BOT_TOKEN), chat ID allowlist from Neo4j
- Git operations: HTTPS with credential helper (`bin/github-auth.sh` configures)

---

*Architecture analysis: 2026-03-05*
