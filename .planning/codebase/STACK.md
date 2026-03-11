# Technology Stack

**Analysis Date:** 2026-03-05

## Languages

**Primary:**
- Python 3.11 - Telegram bot (`telegram-bot/`), API gateway (`api/`), testing framework (`tests/`)
- JavaScript/TypeScript - Frontend sites, CLI tooling, React components

**Secondary:**
- Bash - Infrastructure scripts (`bin/`), session management, git automation
- CSS - Design tokens (`packages/design-system/`)

## Runtime

**Environment:**
- Python 3.11 (specified in `Dockerfile`)
- Node.js 18+ (required by `packages/create-egregore/package.json`)
- Node.js 20 (Netlify build environment from `netlify.toml`)

**Package Manager:**
- Python: pip (no lockfile detected)
- Node.js: npm (package-lock.json present in `blog/`)
- Lockfile: Not consistently used across all projects

## Frameworks

**Core:**
- FastAPI 0.115.0 - API gateway server (`api/main.py`)
- React 18.3.1 / 19.2.0 - Frontend sites (`ascii-oracle/`, `site/`, `egregore-site/`)
- python-telegram-bot 20.7 - Telegram bot integration (`telegram-bot/bot.py`)
- Starlette 0.32.0 - ASGI framework (used by FastAPI and telegram bot)

**Testing:**
- pytest 8.0.0+ - Python test framework (`tests/pyproject.toml`)
- pytest-html 4.0.0+ - HTML test reports

**Build/Dev:**
- Vite 5.4.0 - 7.3.1 - Frontend build tool (multiple versions across projects)
- Uvicorn 0.24.0 / 0.30.0 - ASGI server for FastAPI
- TypeScript 5.6.2 - Type checking for frontend projects
- ESLint 9.x - Linting across frontend projects

## Key Dependencies

**Critical:**
- neo4j 5.27.0 / 5.0.0+ - Knowledge graph database client (`telegram-bot/requirements.txt`, `tests/pyproject.toml`)
- httpx 0.25.2 / 0.27.0 - Async HTTP client (used throughout Python stack)
- supabase 2.10.0 - Alternative storage backend (`api/requirements.txt`)
- python-dotenv 1.0.0 - Environment variable management (used universally)

**Infrastructure:**
- @react-three/fiber 8.15.12 - 3D rendering for ascii-oracle (`ascii-oracle/package.json`)
- three 0.160.0 - WebGL library for 3D graphics
- react-router-dom 6.26.0 / 7.13.0 - Client-side routing
- zustand 4.4.7 - State management (`ascii-oracle/package.json`)
- pydantic 2.0.0+ / 2.9.0 - Data validation in Python (`tests/`, `api/`)
- cryptography 42.0+ - Security primitives (`api/requirements.txt`)
- resend - Email service integration (`api/requirements.txt`)

**AI/ML:**
- Anthropic Claude API - Agent decision-making via direct HTTP calls to `https://api.anthropic.com/v1/messages` (`telegram-bot/bot.py`)
- Model: claude-haiku-4-5-20251001 (primary), claude-sonnet-4-20250514, claude-opus-4-5-20251101 (pricing configs in `telegram-bot/analytics.py`)

## Configuration

**Environment:**
- `.env` files (gitignored) - Personal secrets (`GITHUB_TOKEN`, `EGREGORE_API_KEY`)
- `.env.example` template provided
- `egregore.json` - Committed org configuration (non-secret only: `org_name`, `github_org`, `memory_repo`, `api_url`, `repos[]`)
- TypeScript: `tsconfig.json` with path aliases `@/*` → `src/*`
- ESLint: `eslint.config.js` (flat config format)

**Build:**
- Vite config: `vite.config.js` / `vite.config.ts` (multiple per project)
- Netlify: `netlify.toml` (base: `site/`, publish: `site/dist`)
- Railway: `telegram-bot/railway.json` (Dockerfile-based deployment)
- Docker: `Dockerfile` for telegram bot (Python 3.11-slim base)
- GitHub Actions: `.github/workflows/build-image.yml` (builds egregore-workspace image, pushes to ghcr.io)

## Platform Requirements

**Development:**
- git (required by `start.sh`)
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- jq (JSON processing, required by bash scripts)
- Python 3.11+
- Node.js 18+

**Production:**
- Railway (telegram bot + API gateway deployment target per `telegram-bot/railway.json`)
- Netlify (static site hosting per `netlify.toml`)
- Neo4j AuraDB (referenced in `DEV.md` - two instances: CL private + customer database)
- GitHub Container Registry (Docker image storage per `.github/workflows/build-image.yml`)

---

*Stack analysis: 2026-03-05*
