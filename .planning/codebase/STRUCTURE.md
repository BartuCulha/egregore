# Codebase Structure

**Analysis Date:** 2026-03-05

## Directory Layout

```
egregore/
├── .claude/                # Claude Code configuration
│   ├── commands/          # Slash command specifications (45 commands)
│   └── hooks/             # Lifecycle hooks (boundary-check, branch-guard)
├── .egregore/             # Runtime data (gitignored)
│   ├── notes/             # Scratch notes
│   └── eval-runs/         # Evaluation results
├── .planning/             # GSD planning documents
│   └── codebase/          # This document
├── api/                   # FastAPI gateway
│   ├── services/          # Business logic modules
│   ├── migrations/        # Database migrations
│   ├── main.py            # FastAPI app + routes
│   ├── auth.py            # Multi-tenant auth + org loading
│   ├── models.py          # Pydantic request/response models
│   └── requirements.txt   # Python dependencies
├── bin/                   # Orchestration scripts (32 scripts)
│   └── seeds/             # Graph seed data
├── telegram-bot/          # Telegram interface
│   ├── bot.py             # Telegram webhook + LLM query parsing
│   ├── mcp_server.py      # MCP server for Claude integration
│   └── analytics.py       # Usage tracking
├── tests/                 # API + integration tests (pytest)
│   ├── utils/             # Test helpers
│   └── conftest.py        # Test fixtures
├── site/                  # STALE - do not edit (see CLAUDE.md)
├── packages/              # Installable packages
│   ├── create-egregore/   # npx installer script
│   └── design-system/     # Shared CSS tokens
├── scripts/               # Maintenance scripts
├── skills/                # Experimental skill modules
├── docs/                  # Documentation
├── memory/                # Symlink to shared memory repo (org-specific)
├── egregore.json          # Org config (non-secret, committed)
├── .env                   # Secrets (gitignored)
└── CLAUDE.md              # Main instructions for Claude
```

## Directory Purposes

**.claude/**
- Purpose: Claude Code configuration and command definitions
- Contains: Command specs (markdown), lifecycle hooks (bash), settings.json
- Key files: `commands/activity.md`, `commands/save.md`, `hooks/boundary-check.sh`

**api/**
- Purpose: FastAPI application serving as multi-tenant gateway
- Contains: Routes, auth, services, models, migrations
- Key files: `main.py` (8000+ lines, all routes), `auth.py` (org loading), `services/graph.py` (query injection)

**api/services/**
- Purpose: Business logic layer for API
- Contains: Graph operations, GitHub integration, notifications, Supabase, analytics
- Key files: `graph.py` (query scoping), `github.py` (org provisioning), `notify.py` (Telegram), `guard.py` (query validation)

**bin/**
- Purpose: Client-side orchestration scripts
- Contains: Session lifecycle, git operations, graph/notify wrappers, telemetry
- Key files: `session-start.sh` (entry point), `graph.sh` (Neo4j wrapper), `notify.sh` (Telegram wrapper), `boundary.sh` (path validation)

**telegram-bot/**
- Purpose: Telegram bot for async notifications + natural language queries
- Contains: Webhook handlers, LLM query parsing, MCP server
- Key files: `bot.py` (main bot logic), `mcp_server.py` (Claude MCP integration), `analytics.py`

**tests/**
- Purpose: Pytest test suite for API and flows
- Contains: Unit + integration tests, fixtures, mocks
- Key files: `conftest.py` (fixtures), `test_full_flow.py` (E2E), `test_guard.py` (security)

**packages/create-egregore/**
- Purpose: npm package for one-command setup
- Contains: CLI installer with interactive prompts
- Key files: `bin/cli.js` (entry point), `lib/installer.js`

**memory/** (symlink)
- Purpose: Link to org's shared memory repo (e.g., `curve-labs-memory`)
- Contains: `people/`, `handoffs/`, `knowledge/decisions/`, `knowledge/patterns/`
- Note: Actual location varies by org, defined in `egregore.json`

## Key File Locations

**Entry Points:**
- `.claude/hooks/session-start.sh`: Session initialization (called by Claude Code before first message)
- `bin/session-start.sh`: Main session setup logic (syncs git, loads greeting)
- `api/main.py`: FastAPI application (all API routes)
- `telegram-bot/bot.py`: Telegram bot webhook handler
- `packages/create-egregore/bin/cli.js`: npm installer CLI

**Configuration:**
- `egregore.json`: Org config (name, GitHub org, memory repo URL, API URL, managed repos list)
- `.env`: Personal secrets (GITHUB_TOKEN, EGREGORE_API_KEY)
- `.claude/settings.json`: Claude Code permissions
- `.egregore-state.json`: User state (onboarding progress, preferences)

**Core Logic:**
- `bin/graph.sh`: Neo4j query wrapper (routes to API)
- `bin/notify.sh`: Telegram notification wrapper
- `bin/graph-op.sh`: High-level graph operations (set-topic, update-session)
- `bin/telemetry.sh`: Telemetry buffering + flush
- `api/services/graph.py`: Query scoping via `inject_org_scope()`
- `api/services/guard.py`: Query validation + rate limiting
- `api/auth.py`: Org loading (`load_orgs()`, `load_orgs_from_neo4j()`)

**Testing:**
- `tests/conftest.py`: Shared fixtures (app_client, mock org configs)
- `tests/test_full_flow.py`: E2E setup + invite + accept
- `tests/test_guard.py`: Query guard validation
- `tests/test_org_scope.py`: Org injection correctness

## Naming Conventions

**Files:**
- Bash scripts: `kebab-case.sh` (e.g., `session-start.sh`, `graph-op.sh`)
- Python modules: `snake_case.py` (e.g., `main.py`, `auth.py`)
- Command specs: `kebab-case.md` (e.g., `deep-reflect.md`, `ingest-user-interview.md`)
- Markdown files: `UPPERCASE.md` for docs (README.md, CLAUDE.md, DEV.md)

**Directories:**
- kebab-case: `telegram-bot/`, `ascii-oracle/`, `egregore-site/`
- lowercase: `api/`, `bin/`, `tests/`, `docs/`, `scripts/`
- dot-prefix: `.claude/`, `.egregore/`, `.planning/`, `.github/`

## Where to Add New Code

**New Slash Command:**
- Primary code: `.claude/commands/{command-name}.md`
- Tests: Not applicable (commands are specs, not code)
- Registration: Automatic (Claude reads all files in `.claude/commands/`)

**New API Endpoint:**
- Primary code: `api/main.py` (add route function)
- Models: `api/models.py` (Pydantic request/response classes)
- Service logic: `api/services/{domain}.py` (e.g., `services/github.py`)
- Tests: `tests/test_{feature}.py` with `@pytest.mark.api`

**New Bash Script:**
- Implementation: `bin/{script-name}.sh`
- Make executable: `chmod +x bin/{script-name}.sh`
- Call from commands: Reference as `bash bin/{script-name}.sh` in command specs

**New Graph Query Pattern:**
- Implementation: `api/services/graph.py` or new service module
- Tests: `tests/test_org_scope.py` for scoping, `tests/test_guard.py` for validation
- Client wrapper: May need updates to `bin/graph-op.sh` for convenience

**Utilities:**
- Bash: `bin/{utility-name}.sh` (e.g., `boundary.sh`, `telemetry.sh`)
- Python API: `api/services/{utility}.py` (e.g., `tokens.py`, `keys.py`)
- Python tests: `tests/utils/{utility}.py`

**Frontend (Website):**
- Location: **Separate repo** (`Curve-Labs/egregore-site`, sibling directory `../egregore-site/`)
- Important: `site/`, `egregore-site/`, `site 2/` directories in THIS repo are STALE copies
- Rule: NEVER edit site directories in this repo expecting changes to go live
- Deployment: Only `../egregore-site/` deploys to egregore.xyz via Netlify

**Documentation:**
- User docs: `docs/{topic}.md` (committed, public-facing)
- Developer docs: `DEV.md` (internal operations, not synced to public repo)
- Command help: Inline in `.claude/commands/{command}.md`
- Codebase analysis: `.planning/codebase/{DOC}.md` (generated by GSD)

## Special Directories

**memory/** (symlink)
- Purpose: Link to shared memory git repo
- Generated: No (user creates during setup, resolved from `egregore.json`)
- Committed: Symlink itself yes, target repo no (separate repo)

**.egregore/**
- Purpose: Runtime data (notes, eval runs)
- Generated: Yes (by commands like `/eval`, `/note`)
- Committed: No (gitignored)

**node_modules/** (multiple locations)
- Purpose: npm package dependencies
- Generated: Yes (by `npm install` or `pnpm install`)
- Committed: No (gitignored)

**site/dist/**
- Purpose: Built frontend assets
- Generated: Yes (by `vite build`)
- Committed: No (gitignored)

**tests/.venv/**
- Purpose: Python virtual environment for tests
- Generated: Yes (by `uv` when running tests)
- Committed: No (gitignored)

**api/__pycache__/**
- Purpose: Python bytecode cache
- Generated: Yes (by Python interpreter)
- Committed: No (gitignored)

**bin/seeds/**
- Purpose: Graph seed data for fresh instances
- Generated: No (hand-maintained)
- Committed: Yes

**.planning/**
- Purpose: GSD planning documents (phases, codebase analysis)
- Generated: Yes (by `/gsd:*` commands)
- Committed: Yes (planning state shared across sessions)

---

*Structure analysis: 2026-03-05*
