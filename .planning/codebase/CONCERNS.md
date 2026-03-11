# Codebase Concerns

**Analysis Date:** 2026-03-05

## Tech Debt

**Memory Symlink Fragility:**
- Issue: `memory/` is a symlink to `../curve-labs-memory` (separate repo). If the target doesn't exist or the symlink breaks, critical operations fail silently.
- Files: `memory/` symlink, `bin/session-start.sh`, `.gitignore`
- Impact: Commands like `/handoff`, `/reflect`, and `/save` fail. Session startup may show "Memory symlink missing" errors. New users in multi-instance setups can accidentally cross-contaminate orgs if boundary checks fail.
- Fix approach: Add health check in `session-start.sh` that validates symlink target exists and points to correct repo. Fail fast with actionable error message instead of silent degradation.

**Stale Site Directories:**
- Issue: `site/`, `site 2/`, and `egregore-site/` directories exist in main repo, but CLAUDE.md explicitly states live site deploys from separate `Curve-Labs/egregore-site` repo in sibling directory `../egregore-site/`.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/site/`, `/Users/cemdagdelen/Desktop/CL/egregore/site 2/`, `/Users/cemdagdelen/Desktop/CL/egregore/egregore-site/`
- Impact: Developers may edit wrong directory expecting changes to go live. Creates confusion about source of truth. Wastes disk space with duplicate node_modules.
- Fix approach: Remove stale directories from main repo. Update CLAUDE.md and README.md to clarify deployment flow. Add `.gitignore` entry to prevent re-adding.

**API Main File Complexity:**
- Issue: `api/main.py` is 3,954 lines — far exceeds maintainability threshold.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/main.py`
- Impact: Hard to navigate, test, and modify. High risk of merge conflicts. New endpoints scattered throughout massive file.
- Fix approach: Extract endpoint groups into separate routers: `api/routers/org.py`, `api/routers/graph.py`, `api/routers/notify.py`, `api/routers/github.py`, `api/routers/hosting.py`. Use FastAPI's `APIRouter` for composition.

**Incomplete MCP Server Secret Validation:**
- Issue: TODO comment in `telegram-bot/mcp_server.py:137` — "TODO: validate secret against stored hash". Currently accepts any secret without verification.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/telegram-bot/mcp_server.py`
- Impact: Security risk if MCP server endpoints are exposed. Anyone with network access can invoke Spirit operations without proper authentication.
- Fix approach: Implement bcrypt hash validation against stored secret from Neo4j or Supabase. Reject requests with invalid secrets.

**Unimplemented Task Output Processing:**
- Issue: TODO in `telegram-bot/bot.py:2065` — "TODO: Process outputs (create artifacts, sessions, etc.) based on trust level". Task callbacks are received but not processed.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/telegram-bot/bot.py`
- Impact: Spirits can execute tasks but results are thrown away. No artifact creation, no session tracking, no trust-based gating. Feature appears complete but is a stub.
- Fix approach: Implement trust level checks. High-trust spirits → auto-create artifacts. Medium-trust → pending review. Low-trust → reject with notification.

**Multi-Instance Registry Mutation Risk:**
- Issue: `~/.egregore/instances.json` tracks all Egregore instances on machine. CLAUDE.md warns "Never modify `instances.json`" but `session-start.sh` writes to it on every launch.
- Files: `bin/session-start.sh`, CLAUDE.md Environment Isolation section
- Impact: If multiple sessions start simultaneously (different orgs), concurrent writes can corrupt the registry. Boundary checks depend on this file — corruption = cross-org data leaks.
- Fix approach: Use file locking (flock) around all `instances.json` writes. Add integrity checks on read (validate JSON, check for duplicate entries).

**Session Start Complexity:**
- Issue: `bin/session-start.sh` is 908 lines — handles identity detection, git sync, boundary setup, multi-repo status, health checks, greeting rendering, and more. Single failure point for entire system.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/bin/session-start.sh`
- Impact: Debugging is difficult. Changes risk breaking unrelated features. New developers can't understand full startup flow without reading 900+ lines.
- Fix approach: Extract into modules: `bin/lib/identity.sh`, `bin/lib/sync.sh`, `bin/lib/health.sh`, `bin/lib/greeting.sh`. Source them in main script. Test modules independently.

## Known Bugs

**Org Property Tampering Detection Gap:**
- Symptoms: Guard layer blocks `_org` and `org` as query parameters, but does NOT prevent `SET n.org = "other-org"` in Cypher statements.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/services/guard.py`
- Trigger: Submit query with literal `SET node.org = "attacker-org"` in statement body.
- Workaround: Current `_BLOCKED_TOKENS` only checks for DELETE/DROP/REMOVE. `SET` is allowed for legitimate updates but creates org-scoping bypass.

**Unscoped Person Nodes Leaking:**
- Symptoms: Test warning at `telegram-bot/test_org_isolation.py:599` — "WARNING: {unscoped} Person nodes have NO org property — they leak into all queries!"
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/telegram-bot/test_org_isolation.py`
- Trigger: Creating Person nodes without org property causes them to match queries from all orgs.
- Workaround: Org scoping in `inject_org_scope()` only adds `{org: $_org}` to patterns with labels. Unlabeled refs like `(p)` after `(p:Person)` don't get scoped if `p` is reused without label.

## Security Considerations

**Environment File Present:**
- Risk: `.env` file exists (detected at `/Users/cemdagdelen/Desktop/CL/egregore/.env`). Contains `GITHUB_TOKEN` and `EGREGORE_API_KEY` per documentation.
- Files: `.env` (gitignored)
- Current mitigation: Listed in `.gitignore`. Never read by scripts using `source` (uses `grep | cut` pattern instead to avoid shell injection).
- Recommendations: Add pre-commit hook to block any commit containing `.env` patterns. Add documentation warning about `.env` backup/sharing risks.

**GitHub Client Secret in Environment:**
- Risk: `GITHUB_CLIENT_SECRET` loaded from environment in `api/auth.py`. OAuth flow requires it server-side, but leak = account takeover.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/auth.py`
- Current mitigation: Runs on Railway with secret management. Not stored in repo.
- Recommendations: Add secret rotation documentation. Validate OAuth redirect URIs to prevent authorization code interception.

**Telegram Bot Secret for Spirit Callbacks:**
- Risk: `TELEGRAM_BOT_SECRET` in `api/main.py` used to validate Spirit task callbacks. If leaked, attackers can forge task completions.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/main.py`
- Current mitigation: Secret-based auth on `/spirit/callback` endpoint.
- Recommendations: Add IP allowlisting for Spirit callback endpoints. Implement request signing with timestamp to prevent replay attacks.

**No Rate Limiting on GitHub OAuth:**
- Risk: GitHub OAuth endpoints (`/api/github/callback`, `/api/org/setup`) have no rate limiting. Enables credential stuffing or abuse of org creation.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/main.py`
- Current mitigation: None detected.
- Recommendations: Add rate limiting middleware per IP address. Implement CAPTCHA for org creation flow.

**Unlabeled Node Pattern Bypass:**
- Risk: Guard layer at `api/services/guard.py:102` blocks unlabeled node patterns like `(n)` to prevent org scoping bypass. However, rebound variables (labeled once, reused unlabeled) can leak across orgs.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/services/guard.py`
- Current mitigation: Regex detection of unlabeled patterns with allowlist for labeled vars. System labels (Org, TelegramUser) exempted from scoping.
- Recommendations: Add integration test with adversarial queries. Consider AST-based Cypher parsing instead of regex (brittle).

## Performance Bottlenecks

**Synchronous GitHub API Calls:**
- Problem: `api/services/github.py` uses `httpx` synchronously in async endpoints. Blocks event loop during GitHub operations (clone, fork, PR creation).
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/services/github.py`
- Cause: Functions are `async` but use synchronous HTTP client.
- Improvement path: Switch to `httpx.AsyncClient` for all GitHub API calls. Run heavy operations (clone, fork) in background tasks with status polling.

**Transcript Archive on Session End:**
- Problem: Session end triggers `bin/transcript-archive.sh` which uploads full session transcript to Neo4j. Large transcripts (>1MB) block session close.
- Files: `bin/transcript-archive.sh` (not read but referenced in telemetry docs)
- Cause: Synchronous upload before allowing session to close.
- Improvement path: Background the upload (`&`), return immediately. Session close should be <100ms.

**Health Dashboard Sequential Queries:**
- Problem: `/api/dashboard/health` endpoint runs multiple Neo4j queries sequentially to check org health. Each query waits for previous to complete.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/tests/test_health_dashboard.py` (test exists, implies feature), `/Users/cemdagdelen/Desktop/CL/egregore/api/main.py`
- Cause: Sequential execution instead of parallel.
- Improvement path: Use `asyncio.gather()` to run all health checks concurrently. Reduce latency from O(n) to O(1).

## Fragile Areas

**Onboarding State Machine:**
- Files: `.egregore-state.json`, `.claude/commands/onboarding.md`, `bin/session-start.sh`
- Why fragile: State transitions depend on exact JSON structure. Missing fields, type mismatches, or interrupted flows leave system in broken state. No rollback mechanism.
- Safe modification: Always read full state with `jq`, never partial updates. Add validation schema. Test all exit paths (user abort, network failure, git errors).
- Test coverage: `/onboarding` command has extensive tests (`bin/test-onboarding.sh` — 896 lines). But `.egregore-state.json` corruption scenarios not covered.

**Boundary Check Hook:**
- Files: `.claude/hooks/boundary-check.sh`, `bin/boundary.sh`
- Why fragile: Runs on every tool invocation via PreToolUse hook. If hook crashes, all Claude Code operations break. Symlink resolution edge cases (circular symlinks, deleted targets) can cause infinite loops.
- Safe modification: Never change hook without testing against: broken symlinks, missing `egregore.json`, malformed `repos[]`, circular symlinks, network drives.
- Test coverage: Boundary tests exist (`bin/test-isolation.sh` — 514 lines). But hook crash recovery not tested.

**Graph Sync Merge Logic:**
- Files: `bin/sync-graph.sh`
- Why fragile: Syncs graph state from Neo4j to git files in `memory/`. Uses complex MERGE logic to reconcile graph changes with git history. Conflicts during sync can corrupt both graph and git.
- Safe modification: Never modify MERGE queries without backup. Test with concurrent sessions making conflicting changes.
- Test coverage: No automated tests for sync conflicts detected.

## Scaling Limits

**Neo4j Free Tier for Curve Labs:**
- Current capacity: AuraDB Free (instance `c02bbdac`) — 50K nodes, 175K relationships, 200MB storage per documentation.
- Limit: Curve Labs private instance on free tier. Customer orgs on separate Business Critical instance.
- Scaling path: Migrate CL instance to paid tier when approaching limits. Monitor with `bin/graph.sh schema` and node counts.

**Single Telegram Bot Instance:**
- Current capacity: One bot serves all orgs. Rate limits apply per bot token (not per org).
- Limit: Telegram bot API limits: 30 messages/second per bot, 20 messages/minute per chat.
- Scaling path: Shard by org — each org gets own bot token. Requires bot registration flow in setup.

**Session Start Sequential Repo Sync:**
- Current capacity: Session start syncs `develop` + memory + all managed repos sequentially. With 5 repos, startup takes 5-10 seconds.
- Limit: Becomes unacceptable at ~10 repos (20+ second startup).
- Scaling path: Parallelize git fetch operations with `xargs -P`. Skip sync if repo unchanged (use `git ls-remote` hash check first).

**Shell Script Test Suite Size:**
- Current capacity: 46 slash commands, 31 shell scripts in `bin/`, 16 Python test files. Tests take 2-3 minutes to run full suite.
- Limit: Adding more commands = linear growth in test time. CI already at ~5 min total.
- Scaling path: Parallelize test execution. Split into unit (fast) vs integration (slow) suites. Cache test results by file hash.

## Dependencies at Risk

**Neo4j Driver Mismatch:**
- Risk: `telegram-bot/requirements.txt` pins `neo4j==5.27.0` (Python driver), but API uses HTTP instead of driver. Version mismatch if switching to driver later.
- Impact: Telegram bot uses official driver, API uses raw HTTP. Behavior divergence (connection pooling, retry logic, query format).
- Migration plan: Standardize on HTTP (remove driver dep) OR migrate API to driver (add `neo4j` to `api/requirements.txt`).

**httpx Version Drift:**
- Risk: `api/requirements.txt` has `httpx==0.27.0`, `telegram-bot/requirements.txt` has `httpx==0.25.2`. Major API changes in 0.26.
- Impact: Security patches may not apply uniformly. Behavioral differences in timeout handling.
- Migration plan: Pin to same version across all requirements files. Use shared `requirements.base.txt`.

**Deprecated Blog Dependencies:**
- Risk: `blog/package-lock.json` contains deprecated packages: "glob" versions with "widely publicized security vulnerabilities", "domexception" marked deprecated.
- Impact: Blog subproject at risk. Not clear if blog is active or abandoned.
- Migration plan: Determine if `blog/` directory is still used. If yes, run `npm audit fix`. If no, remove directory.

## Missing Critical Features

**No Rollback for Graph Mutations:**
- Problem: Guard layer blocks DELETE/DETACH/DROP (append-only), but no way to undo incorrect MERGE or SET operations.
- Blocks: Recovering from bad data writes. Testing graph operations safely.
- Priority: Medium — workaround exists (manual Cypher to fix), but risky for non-experts.

**No Backup Strategy Documented:**
- Problem: Neo4j contains critical org data. No documented backup/restore procedures.
- Blocks: Disaster recovery. Migrating between Neo4j instances.
- Priority: High — data loss risk for production orgs.

**Session Replay for Debugging:**
- Problem: Sessions tracked in graph with timestamps, but no way to reconstruct "what happened" from transcripts + graph state.
- Blocks: Debugging issues reported by users ("it broke yesterday"). Understanding emergent behavior in multi-session workflows.
- Priority: Low — manual investigation possible via graph queries.

**No Admin Dashboard:**
- Problem: Railway deployment status, Neo4j health, org metrics only accessible via manual queries or Railway console.
- Blocks: Proactive monitoring. Quick diagnosis of prod issues.
- Priority: Medium — logs exist but scattered.

## Test Coverage Gaps

**API Endpoint Integration Tests:**
- What's not tested: Many endpoints in 3,954-line `api/main.py` lack integration tests. Coverage focused on critical paths (org setup, graph queries, GitHub OAuth).
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/api/main.py`
- Risk: Endpoints like hosting provisioning (`/api/hosting/provision`), VPS setup, Coder deployment not covered by automated tests. Changes can break silently.
- Priority: High

**Frontend Site Tests:**
- What's not tested: `site/` directory (if still active) has no test files detected. React components, setup flow, GitHub integration purely manual testing.
- Files: `/Users/cemdagdelen/Desktop/CL/egregore/site/`
- Risk: Website regressions only caught in production. Setup flow critical for onboarding — failures = lost users.
- Priority: High

**Concurrent Session Behavior:**
- What's not tested: Multiple users in same org, same branch, simultaneous `/save`. Graph query concurrency. Memory repo merge conflicts.
- Files: N/A (scenario testing)
- Risk: Race conditions in branch creation, transcript archiving, graph sync. Likely exists but untested.
- Priority: Medium

**Boundary Check Edge Cases:**
- What's not tested: Circular symlinks, deleted managed repos, malformed `egregore.json` repos array, paths with special characters.
- Files: `bin/boundary.sh`, `.claude/hooks/boundary-check.sh`
- Risk: Hook crash = all tool calls fail. Session becomes unusable.
- Priority: Medium

**Onboarding Interruption:**
- What's not tested: User closes Claude Code mid-onboarding. Network failure during GitHub clone. API key creation succeeds but Neo4j write fails.
- Files: `.claude/commands/onboarding.md`
- Risk: Partial state leaves system broken. User forced to manually clean up `.egregore-state.json`, `.env`, git config.
- Priority: Low — rare but high impact when it happens.

---

*Concerns audit: 2026-03-05*
