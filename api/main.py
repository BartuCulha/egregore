"""
Egregore API Gateway

Sits between egregore instances and shared infrastructure (Neo4j, Telegram).
Each org gets an API key. Secrets stay server-side.

Deploy to Railway alongside telegram-bot.
"""

import os
import json
import logging
import secrets

from dotenv import load_dotenv
load_dotenv(override=False)

from fastapi import FastAPI, Depends, HTTPException, Header, Query, File, Form, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware

from .auth import (
    validate_api_key, validate_admin_github_token, validate_github_token,
    generate_api_key,
    reload_configs, ORG_CONFIGS, ADMIN_USERS,
    load_orgs_from_neo4j, load_orgs, exchange_github_code, GITHUB_CLIENT_ID,
    USE_SUPABASE,
)
from .models import (
    GraphQuery, GraphBatch, NotifySend, NotifyGroup, OrgRegister,
    OrgSetup, OrgJoin, OrgTelegram, GitHubCallback, SetupOrgsResponse,
    OrgInvite, OrgAcceptInvite, UserEnsure, UserProfileUpdate,
    WaitlistAdd, WaitlistApprove, HealthCheckin, RemoveMemberResponse,
    HostingProvision, HostingUser, UserKeysUpdate,
    GoogleOAuthCallback, GooglePromote,
)
from .services.graph import execute_query, execute_batch, execute_system_query, get_schema, test_connection
from .services.notify import send_message, send_group, test_notify, generate_bot_invite_link, create_group_invite_link
from .services import github as gh
from .services.tokens import create_token, claim_token, create_invite_token, peek_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Egregore API",
    description="API gateway for Egregore shared infrastructure",
    version="1.0.0",
)

# CORS: Only allow browser requests from known origins.
# CLI tools (bin/graph.sh, bin/notify.sh, create-egregore) use curl, not browsers.
_cors_origins = os.environ.get(
    "CORS_ORIGINS",
    "https://egregore-core.netlify.app,https://egregore.xyz,https://www.egregore.xyz"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.on_event("startup")
async def startup():
    """Load org configs on startup (from Supabase if enabled, else Neo4j)."""
    await load_orgs()


@app.post("/api/admin/reload")
async def admin_reload(authorization: str = Header(...)):
    """Reload ORG_CONFIGS. Requires any valid API key."""
    key = authorization.replace("Bearer ", "").strip()
    # Check in-memory keys first
    valid = any(org.get("api_key") == key for org in ORG_CONFIGS.values())
    # If USE_SUPABASE, also validate via hash
    if not valid and USE_SUPABASE:
        try:
            from .services.supabase import validate_api_key as sb_validate
            valid = sb_validate(key) is not None
        except Exception:
            pass
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid API key")
    await load_orgs()
    return {"status": "ok", "orgs_loaded": len(ORG_CONFIGS)}


# =============================================================================
# GRAPH ENDPOINTS
# =============================================================================


@app.post("/api/graph/query")
async def graph_query(body: GraphQuery, org: dict = Depends(validate_api_key)):
    """Execute a Cypher query scoped to the org."""
    result = await execute_query(org, body.statement, body.parameters)
    if isinstance(result, dict) and result.get("error"):
        status = 429 if result.get("rate_limited") else 400
        raise HTTPException(status_code=status, detail=result["error"])
    return result


@app.post("/api/graph/batch")
async def graph_batch(body: GraphBatch, org: dict = Depends(validate_api_key)):
    """Execute multiple Cypher queries concurrently, scoped to the org."""
    queries = [{"statement": q.statement, "parameters": q.parameters} for q in body.queries]
    results = await execute_batch(org, queries)
    # If the first result has a batch-level error, raise it
    if len(results) == 1 and isinstance(results[0], dict) and results[0].get("error"):
        r = results[0]
        status = 429 if r.get("rate_limited") else 400
        raise HTTPException(status_code=status, detail=r["error"])
    return {"results": results}


@app.get("/api/graph/schema")
async def graph_schema(org: dict = Depends(validate_api_key)):
    """Get the Neo4j schema."""
    return await get_schema(org)


@app.get("/api/graph/test")
async def graph_test(org: dict = Depends(validate_api_key)):
    """Test Neo4j connectivity."""
    result = await test_connection(org)
    if result["status"] != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "Connection failed"))
    return result


# =============================================================================
# ACTIVITY DASHBOARD
# =============================================================================


@app.get("/api/activity/dashboard")
async def activity_dashboard(
    github_username: str = Query(..., description="GitHub username to resolve Person"),
    org: dict = Depends(validate_api_key),
):
    """All activity dashboard queries in one call. Replaces 15+ client-side graph.sh calls."""
    from .services.activity import get_activity_dashboard
    return await get_activity_dashboard(org, github_username)


@app.get("/api/personal/dashboard")
async def personal_dashboard(
    github_username: str = Query(..., description="GitHub username to resolve Person"),
    time_range: str = Query("P7D", description="Neo4j duration string: P1D, P7D, P30D, P365D"),
    session_id: str = Query(None, description="Current session ID for auto-capture lookup"),
    org: dict = Depends(validate_api_key),
):
    """Personal dashboard: sessions, todos, quests, handoffs, open threads in one call."""
    from .services.dashboard import get_personal_dashboard
    return await get_personal_dashboard(org, github_username, time_range, session_id)


# =============================================================================
# USER SYNC
# =============================================================================


@app.post("/api/user/ensure")
async def user_ensure(body: UserEnsure, org: dict = Depends(validate_api_key)):
    """Ensure user + membership exist in Supabase. Idempotent.

    Called by session-start.sh and /invite to sync Person creation to Supabase.
    """
    from .services.supabase import upsert_user, add_membership

    if not USE_SUPABASE:
        return {"status": "skipped", "reason": "supabase disabled"}

    try:
        user = upsert_user(
            github_username=body.github_username,
            github_name=body.github_name,
            telegram_username=body.telegram_username,
            telegram_id=body.telegram_id,
        )
        membership = add_membership(
            org_slug=org["slug"],
            github_username=body.github_username,
            display_name=body.display_name,
            member_role=body.member_role,
            focus=body.focus,
            work_style=body.work_style,
            consent_session_tracking=body.consent_session_tracking,
            consent_transcript_sharing=body.consent_transcript_sharing,
            consent_telemetry=body.consent_telemetry,
            contact_preference=body.contact_preference,
        )
        return {"status": "ok", "user_id": user.get("id"), "membership_id": membership.get("id")}
    except Exception as e:
        # Non-fatal — Neo4j is the source of truth, Supabase is supplementary
        return {"status": "error", "detail": str(e)}


# =============================================================================
# NOTIFY ENDPOINTS
# =============================================================================


@app.post("/api/notify/send")
async def notify_send(body: NotifySend, org: dict = Depends(validate_api_key)):
    """Send a message to a person (DM if possible, group fallback)."""
    result = await send_message(org, body.to, body.message)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("detail"))
    return result


@app.post("/api/notify/group")
async def notify_group(body: NotifyGroup, org: dict = Depends(validate_api_key)):
    """Send a message to the org's group chat."""
    result = await send_group(org, body.message)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("detail"))
    return result


@app.get("/api/notify/test")
async def notify_test(org: dict = Depends(validate_api_key)):
    """Test Telegram connectivity."""
    result = await test_notify(org)
    if result["status"] != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail"))
    return result


# =============================================================================
# KEY RETRIEVAL (for existing users after updates)
# =============================================================================


@app.get("/api/org/{slug}/key")
async def org_get_key(slug: str, authorization: str = Header(...)):
    """Return the org's API key if the caller is a verified member.

    Used by session-start.sh to auto-provision EGREGORE_API_KEY for existing
    users who pull an update but don't have the key in .env yet.
    Auth: GitHub token (not API key — they don't have one yet).
    """
    import httpx

    github_token = authorization.replace("Bearer ", "").strip()
    if not github_token:
        raise HTTPException(status_code=401, detail="Missing GitHub token")

    org = ORG_CONFIGS.get(slug)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")

    github_org = org.get("github_org", "")
    if not github_org:
        raise HTTPException(status_code=500, detail="Org has no github_org configured")

    # Verify caller is a member of the GitHub org (or owner of personal account)
    async with httpx.AsyncClient() as client:
        # Check org membership
        resp = await client.get(
            f"https://api.github.com/orgs/{github_org}/members",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )

        if resp.status_code == 200:
            # Get the caller's username
            user_resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {github_token}"},
                timeout=10.0,
            )
            if user_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid GitHub token")

            username = user_resp.json().get("login", "")
            members = [m["login"] for m in resp.json()]
            if username not in members:
                raise HTTPException(status_code=403, detail="Not a member of this org")
        elif resp.status_code == 404:
            # Might be a personal account — check if they own the repo
            user_resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"token {github_token}"},
                timeout=10.0,
            )
            if user_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Invalid GitHub token")

            username = user_resp.json().get("login", "")
            if username.lower() != github_org.lower():
                raise HTTPException(status_code=403, detail="Not authorized for this org")
        else:
            raise HTTPException(status_code=401, detail="Could not verify org membership")

    api_key = await _get_org_api_key(org, slug)
    if not api_key:
        raise HTTPException(status_code=503, detail="API key not available. Contact your team admin.")

    return {"api_key": api_key, "org_slug": slug}


# =============================================================================
# ORG MANAGEMENT (legacy)
# =============================================================================


@app.post("/api/org/register")
async def org_register(body: OrgRegister, authorization: str = Header(...)):
    """Register a new org. Requires a valid GitHub token for verification."""
    import httpx

    # Verify the GitHub token is valid
    github_token = authorization.replace("Bearer ", "").strip()

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    github_user = resp.json()

    # Generate org slug from github_org (lowercase, alphanumeric + hyphens)
    slug = body.github_org.lower().replace(" ", "-")

    # Check if org already exists
    if slug in ORG_CONFIGS:
        existing_key = await _get_org_api_key(ORG_CONFIGS[slug], slug)
        return {
            "api_key": existing_key,
            "org_slug": slug,
            "status": "existing",
        }

    # Generate API key
    api_key = generate_api_key(slug)

    # New orgs go to the shared egregore-core Neo4j instance (not CL's private one)
    default_neo4j_host = os.environ.get("EGREGORE_NEO4J_HOST", "")
    default_neo4j_user = os.environ.get("EGREGORE_NEO4J_USER", "neo4j")
    default_neo4j_password = os.environ.get("EGREGORE_NEO4J_PASSWORD", "")
    if not default_neo4j_host:
        raise HTTPException(
            status_code=503,
            detail="EGREGORE_NEO4J_HOST not configured. Cannot create org without a database.",
        )
    default_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    new_org = {
        "slug": slug,
        "api_key": api_key,
        "org_name": body.org_name,
        "github_org": body.github_org,
        "neo4j_host": default_neo4j_host,
        "neo4j_user": default_neo4j_user,
        "neo4j_password": default_neo4j_password,
        "telegram_bot_token": default_bot_token,
        "telegram_chat_id": body.telegram_chat_id or "",
        "registered_by": github_user.get("login", ""),
    }

    # Persist to Supabase if enabled, then in-memory
    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            sb.create_org(
                slug=slug, name=body.org_name, github_org=body.github_org,
                neo4j_host=default_neo4j_host, neo4j_user=default_neo4j_user,
                neo4j_password=default_neo4j_password,
                telegram_chat_id=body.telegram_chat_id,
                created_by=github_user.get("login", ""),
            )
            sb.create_api_key(slug, api_key)
            sb.upsert_user(github_user.get("login", ""), github_name=github_user.get("name"))
            sb.add_membership(slug, github_user.get("login", ""), role="admin")
        except Exception as e:
            logger.error(f"Supabase org register failed: {e}")
            raise HTTPException(status_code=503, detail="Failed to persist org. Please retry.")

    ORG_CONFIGS[slug] = new_org

    # Create Org node in Neo4j (keep for knowledge graph — include api_key for fallback retrieval)
    try:
        await execute_query(new_org, """
            MERGE (o:Org {id: $_org})
            SET o.name = $name, o.github_org = $github_org, o.created_by = $created_by,
                o.api_key = $api_key
            RETURN o.id
        """, {"name": body.org_name, "github_org": body.github_org,
              "created_by": github_user.get("login", ""), "api_key": api_key})
    except Exception as e:
        logger.warning(f"Neo4j Org node creation failed (non-fatal with Supabase): {e}")

    logger.info(f"Registered new org: {slug} by {github_user.get('login')}")

    return {
        "api_key": api_key,
        "org_slug": slug,
        "status": "created",
    }


@app.get("/api/org/status")
async def org_status(org: dict = Depends(validate_api_key)):
    """Get org config (non-sensitive fields)."""
    return {
        "org_name": org.get("org_name", ""),
        "github_org": org.get("github_org", ""),
        "slug": org.get("slug", ""),
        "has_telegram": bool(org.get("telegram_bot_token")),
        "has_neo4j": bool(org.get("neo4j_host")),
    }


# =============================================================================
# SETUP FLOW — Frictionless installation
# =============================================================================


@app.post("/api/auth/github/callback")
async def github_callback(body: GitHubCallback):
    """Exchange GitHub OAuth code for access token."""
    token = await exchange_github_code(body.code)
    user = await gh.get_user(token)
    return {"github_token": token, "user": user}


@app.get("/api/org/setup/orgs")
async def setup_orgs(authorization: str = Header(...)):
    """Detect user's orgs and their Egregore status."""
    token = authorization.replace("Bearer ", "").strip()

    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    orgs = await gh.list_orgs(token)

    # Check each org for egregore instances + membership status
    org_results = []
    for org in orgs:
        instances = await gh.list_egregore_instances(token, org["login"])
        has_egregore = len(instances) > 0
        role = await gh.get_org_membership(token, org["login"])
        is_member = False
        if has_egregore:
            is_member = await gh.repo_exists(token, org["login"], f"{org['login']}-memory")
        org_results.append({
            "login": org["login"],
            "name": org["name"],
            "has_egregore": has_egregore,
            "is_member": is_member,
            "can_setup": True,
            "role": role or "member",
            "avatar_url": org.get("avatar_url", ""),
            "instances": instances,
        })

    personal_instances = await gh.list_egregore_instances(token, user["login"])
    personal_has = len(personal_instances) > 0
    personal_member = False
    if personal_has:
        personal_member = await gh.repo_exists(token, user["login"], f"{user['login']}-memory")

    return {
        "user": user,
        "orgs": org_results,
        "personal": {
            "login": user["login"],
            "has_egregore": personal_has,
            "is_member": personal_member,
            "can_setup": True,
            "instances": personal_instances,
        },
    }


@app.get("/api/org/setup/repos")
async def setup_repos(org: str, authorization: str = Header(...)):
    """List repos for an org, for the repo picker during setup."""
    token = authorization.replace("Bearer ", "").strip()
    try:
        await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")
    repos = await gh.list_org_repos(token, org)
    return {"repos": repos}


@app.post("/api/org/setup")
async def org_setup(body: OrgSetup, authorization: str = Header(...)):
    """Founder: full org setup. Generates from template, creates memory, bootstraps Neo4j, returns setup token."""
    token = authorization.replace("Bearer ", "").strip()

    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    # Auto-detect personal vs org account (don't trust client)
    is_personal = not await gh.is_org(token, body.github_org)
    target_org = None if is_personal else body.github_org
    owner = user["login"] if is_personal else body.github_org
    base_slug = owner.lower().replace("-", "").replace(" ", "")

    # Compute instance slug and repo name
    if body.instance_name:
        instance_suffix = body.instance_name.lower().replace(" ", "-")
        slug = f"{base_slug}-{instance_suffix}"
        repo_name = f"egregore-{instance_suffix}"
    else:
        slug = base_slug
        repo_name = "egregore-core"

    # Memory repo: use slug prefix for additional instances, owner prefix for first (backwards compat)
    if body.instance_name:
        memory_repo_name = f"{slug}-memory"
    else:
        memory_repo_name = f"{owner}-memory"

    # 1. Create repo from template (private by default — forks of public repos can't be private)
    repo_already_exists = await gh.repo_exists(token, owner, repo_name)
    if repo_already_exists:
        logger.info(f"{repo_name} already exists for {owner} — continuing setup")
    else:
        logger.info(f"Creating {repo_name} from template for {owner}")
        await gh.generate_from_template(token, owner, repo_name, private=True)

        # Wait for repo to be ready
        if not await gh.wait_for_repo(token, owner, repo_name):
            raise HTTPException(status_code=504, detail="Repo creation timed out — try again")

    # 3. Create memory repo
    logger.info(f"Creating {memory_repo_name} for {owner}")
    try:
        await gh.create_repo(token, memory_repo_name, target_org)
    except ValueError as e:
        error_text = str(e).lower()
        if "422" in error_text and ("already exists" in error_text or "name already" in error_text):
            logger.info(f"{memory_repo_name} already exists for {owner} — continuing")
        else:
            raise

    # 4. Verify memory repo exists (catches silent creation failures)
    if not await gh.repo_exists(token, owner, memory_repo_name):
        logger.error(f"Memory repo {owner}/{memory_repo_name} does not exist after creation attempt")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create memory repo {owner}/{memory_repo_name}. "
                   f"Check that your GitHub token has the 'repo' scope.",
        )

    # 5. Init memory structure
    await gh.init_memory_structure(token, owner, memory_repo_name)

    # 5. Generate API key + store org
    api_key = generate_api_key(slug)
    api_url = os.environ.get("EGREGORE_API_URL", "https://egregore-production-55f2.up.railway.app")

    # New customer orgs use the shared customer Neo4j instance (EGREGORE_NEO4J_HOST).
    # CL's private instance (NEO4J_HOST) is only for the curvelabs org.
    default_neo4j_host = os.environ.get("EGREGORE_NEO4J_HOST", "")
    default_neo4j_user = os.environ.get("EGREGORE_NEO4J_USER", "neo4j")
    default_neo4j_password = os.environ.get("EGREGORE_NEO4J_PASSWORD", "")
    if not default_neo4j_host:
        raise HTTPException(
            status_code=503,
            detail="EGREGORE_NEO4J_HOST not configured. Cannot create org without a database.",
        )
    default_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    new_org = {
        "api_key": api_key,
        "org_name": body.org_name,
        "github_org": owner,
        "neo4j_host": default_neo4j_host,
        "neo4j_user": default_neo4j_user,
        "neo4j_password": default_neo4j_password,
        "telegram_bot_token": default_bot_token,
        "telegram_chat_id": body.telegram_chat_id or "",
        "slug": slug,
        "transcript_sharing": body.transcript_sharing,
    }

    # 6. Persist org — Supabase first (if enabled), then Neo4j, then in-memory
    managed_repos_str = ",".join(body.repos) if body.repos else ""
    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            sb.create_org(
                slug=slug, name=body.org_name, github_org=owner,
                neo4j_host=default_neo4j_host, neo4j_user=default_neo4j_user,
                neo4j_password=default_neo4j_password,
                telegram_chat_id=body.telegram_chat_id,
                created_by=user["login"],
                transcript_sharing=body.transcript_sharing,
                repo_name=repo_name,
                managed_repos=managed_repos_str,
            )
            sb.revoke_api_key(slug)  # Idempotent: revoke any old keys before creating new
            sb.create_api_key(slug, api_key)
            sb.upsert_user(user["login"], github_name=user.get("name"), avatar_url=user.get("avatar_url"))
            sb.add_membership(slug, user["login"], role="admin",
                              coder_username=user["login"] if body.hosting else None)
            logger.info(f"Supabase org bootstrapped for {slug}")
        except Exception as e:
            logger.error(f"Supabase org bootstrap failed: {e}")
            raise HTTPException(status_code=503, detail="Failed to persist org. Please retry.")

    try:
        neo4j_result = await execute_query(new_org, """
            MERGE (o:Org {id: $_org})
            SET o.name = $name, o.github_org = $github_org, o.api_key = $api_key,
                o.created_by = $created_by, o.transcript_sharing = $transcript_sharing
        """, {"name": body.org_name, "github_org": owner, "api_key": api_key,
              "created_by": user["login"], "transcript_sharing": body.transcript_sharing})
        if isinstance(neo4j_result, dict) and "error" in neo4j_result:
            if not USE_SUPABASE:
                raise HTTPException(status_code=503, detail="Failed to persist org. Please retry.")
            logger.warning(f"Neo4j Org node creation failed (non-fatal with Supabase): {neo4j_result}")
        else:
            logger.info(f"Neo4j Org node bootstrapped for {slug}")
    except HTTPException:
        raise
    except Exception as e:
        if not USE_SUPABASE:
            logger.error(f"Neo4j Org bootstrap exception: {e}")
            raise HTTPException(status_code=503, detail="Failed to persist org. Please retry.")
        logger.warning(f"Neo4j Org node creation failed (non-fatal with Supabase): {e}")

    # Add to memory after successful persistence
    ORG_CONFIGS[slug] = new_org

    # 7. Update egregore.json in the generated repo
    memory_url = f"https://github.com/{owner}/{memory_repo_name}.git"
    try:
        await gh.update_egregore_json(
            token, owner, repo_name,
            body.org_name, owner, memory_repo_name,
            api_url,
            slug=slug,
            repos=body.repos,
        )
    except ValueError as e:
        logger.warning(f"Failed to update egregore.json: {e}")

    # 7b. Sync develop branch to main (fork inherits upstream branches with stale config)
    try:
        synced = await gh.sync_branch_to_main(token, owner, repo_name, "develop")
        if synced:
            logger.info(f"Synced develop → main for {owner}/{repo_name}")
        else:
            logger.warning(f"Failed to sync develop branch for {owner}/{repo_name}")
    except Exception as e:
        logger.warning(f"develop branch sync error: {e}")

    # 8. Telegram invite link
    telegram_invite = generate_bot_invite_link(slug)

    # 9. Generate setup token
    fork_url = f"https://github.com/{owner}/{repo_name}.git"
    setup_token = create_token({
        "fork_url": fork_url,
        "memory_url": memory_url,
        "api_key": api_key,
        "api_url": api_url,
        "org_name": body.org_name,
        "github_org": owner,
        "github_token": token,
        "slug": slug,
        "repos": body.repos,
        "repo_name": repo_name,
        "transcript_sharing": body.transcript_sharing,
    })

    logger.info(f"Org setup complete: {slug} by {user['login']}")

    result = {
        "setup_token": setup_token,
        "fork_url": fork_url,
        "memory_url": memory_url,
        "org_slug": slug,
        "telegram_invite_link": telegram_invite,
    }

    # 10. Provision hosted VPS if requested
    if body.hosting:
        try:
            from .services.hosting import provision_vps

            vps_result = await provision_vps(
                org_slug=slug,
                org_name=body.org_name,
                github_org=owner,
                repo_name=repo_name,
                fork_url=fork_url,
                memory_url=memory_url,
                api_url=api_url,
                egregore_api_key=api_key,
                managed_repos=managed_repos_str,
                server_type=body.server_type,
                github_token=token,
            )

            if vps_result.get("error"):
                logger.warning(f"VPS provisioning failed for {slug}: {vps_result['error']}")
                result["hosting_status"] = "failed"
                result["hosting_error"] = vps_result["error"]
            else:
                result["hosting_status"] = "provisioning"
                result["hosting_ip"] = vps_result.get("ip", "")
                result["hosting_coder_url"] = vps_result.get("coder_url", "")

                # Store VPS info in Supabase
                if USE_SUPABASE:
                    try:
                        from .services import supabase as sb
                        sb.get_client().table("orgs").update({
                            "hosting_ip": vps_result["ip"],
                            "hosting_server_id": str(vps_result.get("server_id", "")),
                            "hosting_coder_url": vps_result.get("coder_url", ""),
                            "hosting_enabled": True,
                        }).eq("slug", slug).execute()
                    except Exception as e:
                        logger.warning(f"Failed to store hosting info: {e}")

                logger.info(f"VPS provisioning started for {slug}")
        except Exception as e:
            logger.error(f"VPS provisioning error for {slug}: {e}")
            result["hosting_status"] = "failed"
            result["hosting_error"] = str(e)

    return result


@app.post("/api/org/join")
async def org_join(body: OrgJoin, authorization: str = Header(...)):
    """Joiner: join an existing org. Verifies access, returns setup token."""
    token = authorization.replace("Bearer ", "").strip()

    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    owner = body.github_org

    # Verify repo exists
    if not await gh.repo_exists(token, owner, body.repo_name):
        raise HTTPException(status_code=404, detail=f"No Egregore setup found for {owner} ({body.repo_name})")

    # Read egregore.json from repo to get config
    config_raw = await gh.get_file_content(token, owner, body.repo_name, "egregore.json")
    if not config_raw:
        raise HTTPException(status_code=404, detail="egregore.json not found in repo")

    config = json.loads(config_raw)
    memory_repo = config.get("memory_repo", f"{owner}-memory")

    # Determine memory URL
    if memory_repo.startswith("http"):
        memory_url = memory_repo
    else:
        memory_url = f"https://github.com/{owner}/{memory_repo}.git"

    # Verify user has access to memory repo — create if missing and user is owner/admin
    memory_repo_name = memory_repo.split("/")[-1].replace(".git", "") if "/" in memory_repo else memory_repo
    if not await gh.repo_exists(token, owner, memory_repo_name):
        # Check if user can create it (personal account owner or org admin)
        can_create = (owner == user["login"]) or (await gh.get_org_membership(token, owner) == "admin")
        if can_create:
            logger.info(f"Memory repo {owner}/{memory_repo_name} missing — creating for incomplete setup")
            try:
                target_org = None if owner == user["login"] else owner
                await gh.create_repo(token, memory_repo_name, target_org)
                await gh.init_memory_structure(token, owner, memory_repo_name)
            except Exception as e:
                logger.error(f"Failed to create memory repo: {e}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Memory repo {owner}/{memory_repo_name} doesn't exist and couldn't be created: {e}",
                )
        else:
            raise HTTPException(
                status_code=403,
                detail=f"You don't have access to {owner}/{memory_repo_name}. Ask your team to add you.",
            )

    fork_url = f"https://github.com/{owner}/{body.repo_name}.git"
    api_url = config.get("api_url", "")

    # Look up org's API key — slug must come from egregore.json config
    slug = config.get("slug")
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="egregore.json is missing 'slug' field. Run setup again or add it manually.",
        )
    repos = config.get("repos", [])
    org_config = ORG_CONFIGS.get(slug)
    api_key = await _get_org_api_key(org_config, slug) if org_config else ""

    # Register user + membership in Supabase immediately (don't defer to session-start)
    # If org has hosting, set coder_username so terminal URL works on dashboard
    coder_username_for_join = None
    if USE_SUPABASE:
        try:
            from .services.supabase import upsert_user, add_membership
            upsert_user(
                github_username=user["login"],
                github_name=user.get("name"),
                avatar_url=user.get("avatar_url"),
            )
            # Check if org has hosting — set coder_username if so
            try:
                coder_url, coder_token = await _get_coder_credentials(slug)
                if coder_url and coder_token:
                    coder_username_for_join = user["login"]
                    from .services import supabase as sb_join
                    org_data_row = sb_join.get_client().table("orgs").select(
                        "name, github_org, repo_name, managed_repos"
                    ).eq("slug", slug).execute()
                    org_data = org_data_row.data[0] if org_data_row.data else {}
                    from .services.coder import CoderClient
                    coder_client = CoderClient(coder_url, coder_token)
                    try:
                        await coder_client.create_user(
                            username=user["login"],
                            email=f"{user['login']}@users.noreply.github.com",
                            name=user.get("name", user["login"]),
                        )
                        await coder_client.create_workspace(
                            owner=user["login"],
                            org_slug=slug,
                            org_name=org_data.get("name", slug),
                            github_org=org_data.get("github_org", ""),
                            repo_name=org_data.get("repo_name", ""),
                            managed_repos=org_data.get("managed_repos", ""),
                        )
                        logger.info(f"Join: created Coder user+workspace for {user['login']} on {slug}")
                    except Exception as ce:
                        logger.warning(f"Join: Coder setup failed for {user['login']}: {ce}")
            except Exception as he:
                logger.warning(f"Join: hosting check failed: {he}")

            add_membership(slug, user["login"], role="member",
                          coder_username=coder_username_for_join)
            logger.info(f"Join: registered {user['login']} in Supabase for {slug}")
        except Exception as e:
            logger.warning(f"Join: Supabase registration failed for {user['login']}: {e}")

    # Generate Telegram group invite if configured
    telegram_group_link = None
    if org_config:
        try:
            telegram_group_link = await create_group_invite_link(org_config)
        except Exception:
            pass

    setup_token = create_token({
        "fork_url": fork_url,
        "memory_url": memory_url,
        "api_key": api_key,
        "api_url": api_url,
        "org_name": config.get("org_name", owner),
        "github_org": owner,
        "github_token": token,
        "github_username": user["login"],
        "github_name": user.get("name", user["login"]),
        "slug": slug,
        "repos": repos,
        "repo_name": body.repo_name,
        "telegram_group_link": telegram_group_link,
    })

    return {
        "setup_token": setup_token,
        "fork_url": fork_url,
        "memory_url": memory_url,
        "org_slug": slug,
        "telegram_group_link": telegram_group_link,
    }


@app.get("/api/org/claim/{token}")
async def org_claim(token: str):
    """Redeem a one-time setup token. Returns everything npx needs."""
    data = claim_token(token)
    if not data:
        raise HTTPException(status_code=404, detail="Token expired or already used")
    return data


@app.get("/api/org/install/{token}")
async def org_install_script(token: str):
    """Return a bash install script for users without Node.js.

    Usage: curl -fsSL https://egregore-core.netlify.app/api/org/install/st_xxx | bash
    """
    from fastapi.responses import PlainTextResponse

    # Peek to validate token exists (don't consume — the script will claim it)
    data = peek_token(token)
    if not data:
        return PlainTextResponse("echo 'Error: Token expired or invalid.'; exit 1", status_code=404)

    api_url = os.environ.get("EGREGORE_API_URL", "https://egregore-production-55f2.up.railway.app")

    script = f"""#!/bin/bash
set -euo pipefail

echo ""
echo "  Installing Egregore..."
echo ""

# Check for git
if ! command -v git &>/dev/null; then
  echo "  Error: git is required. Install it first."
  exit 1
fi

# Check for curl or wget
if ! command -v curl &>/dev/null; then
  echo "  Error: curl is required."
  exit 1
fi

# Check for jq (optional but helpful)
HAS_JQ=false
if command -v jq &>/dev/null; then
  HAS_JQ=true
fi

# Claim the setup token
echo "  [1/5] Claiming setup token..."
RESPONSE=$(curl -fsSL "{api_url}/api/org/claim/{token}")

if [ -z "$RESPONSE" ]; then
  echo "  Error: Token expired or already used."
  exit 1
fi

# Parse JSON response (with or without jq)
if $HAS_JQ; then
  FORK_URL=$(echo "$RESPONSE" | jq -r '.fork_url')
  MEMORY_URL=$(echo "$RESPONSE" | jq -r '.memory_url')
  GITHUB_TOKEN=$(echo "$RESPONSE" | jq -r '.github_token // empty')
  ORG_NAME=$(echo "$RESPONSE" | jq -r '.org_name')
  GITHUB_ORG=$(echo "$RESPONSE" | jq -r '.github_org')
  API_KEY=$(echo "$RESPONSE" | jq -r '.api_key')
  API_URL=$(echo "$RESPONSE" | jq -r '.api_url')
  SLUG=$(echo "$RESPONSE" | jq -r '.slug')
  TELEGRAM_LINK=$(echo "$RESPONSE" | jq -r '.telegram_group_link // empty')
  NEEDS_CLI_AUTH=$(echo "$RESPONSE" | jq -r '.needs_cli_auth // empty')
else
  # Fallback: extract with grep/sed (works for simple JSON)
  extract() {{ echo "$RESPONSE" | grep -o "\\"$1\\":\\"[^\\"]*\\"" | head -1 | sed 's/.*:"//;s/"$//'; }}
  FORK_URL=$(extract fork_url)
  MEMORY_URL=$(extract memory_url)
  GITHUB_TOKEN=$(extract github_token)
  ORG_NAME=$(extract org_name)
  GITHUB_ORG=$(extract github_org)
  API_KEY=$(extract api_key)
  API_URL=$(extract api_url)
  SLUG=$(extract slug)
  TELEGRAM_LINK=$(extract telegram_group_link)
  NEEDS_CLI_AUTH=$(echo "$RESPONSE" | grep -o '"needs_cli_auth":true' | head -1)
fi

# If no github_token (joiner flow), run device flow to get one
if [ -z "$GITHUB_TOKEN" ] || [ "$NEEDS_CLI_AUTH" = "true" ]; then
  echo "  Sign in with GitHub to complete setup."
  echo ""

  CLIENT_ID="Ov23lizB4nYEeIRsHTdb"
  DEVICE_SCOPE="repo,read:org"

  DEVICE_RESP=$(curl -s -X POST "https://github.com/login/device/code" \\
    -H "Accept: application/json" \\
    -d "client_id=$CLIENT_ID&scope=$DEVICE_SCOPE")

  if $HAS_JQ; then
    DEVICE_CODE=$(echo "$DEVICE_RESP" | jq -r '.device_code')
    USER_CODE=$(echo "$DEVICE_RESP" | jq -r '.user_code')
    VERIFY_URL=$(echo "$DEVICE_RESP" | jq -r '.verification_uri_complete // .verification_uri')
    POLL_INTERVAL=$(echo "$DEVICE_RESP" | jq -r '.interval')
  else
    DEVICE_CODE=$(echo "$DEVICE_RESP" | grep -o '"device_code":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
    USER_CODE=$(echo "$DEVICE_RESP" | grep -o '"user_code":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
    VERIFY_URL=$(echo "$DEVICE_RESP" | grep -o '"verification_uri_complete":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
    [ -z "$VERIFY_URL" ] && VERIFY_URL=$(echo "$DEVICE_RESP" | grep -o '"verification_uri":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
    POLL_INTERVAL=$(echo "$DEVICE_RESP" | grep -o '"interval":[0-9]*' | head -1 | sed 's/.*://')
  fi

  if [ -z "$DEVICE_CODE" ] || [ "$DEVICE_CODE" = "null" ]; then
    echo "  Error: Failed to start GitHub device flow."
    exit 1
  fi

  # Copy code to clipboard and open browser
  if command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$USER_CODE" | pbcopy
    echo "  Code copied to clipboard: $USER_CODE"
  else
    echo "  Your code: $USER_CODE"
  fi
  echo "  Opening browser — paste the code and authorize."
  echo ""

  if command -v open >/dev/null 2>&1; then
    open "$VERIFY_URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$VERIFY_URL"
  else
    echo "  Open this URL manually: $VERIFY_URL"
  fi

  # Poll for token
  POLL_INTERVAL=${{POLL_INTERVAL:-5}}
  ELAPSED=0
  TIMEOUT=300
  while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    TOKEN_RESP=$(curl -s -X POST "https://github.com/login/oauth/access_token" \\
      -H "Accept: application/json" \\
      -d "client_id=$CLIENT_ID&device_code=$DEVICE_CODE&grant_type=urn:ietf:params:oauth:grant-type:device_code")

    if $HAS_JQ; then
      GITHUB_TOKEN=$(echo "$TOKEN_RESP" | jq -r '.access_token // empty')
      TOKEN_ERROR=$(echo "$TOKEN_RESP" | jq -r '.error // empty')
    else
      GITHUB_TOKEN=$(echo "$TOKEN_RESP" | grep -o '"access_token":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
      TOKEN_ERROR=$(echo "$TOKEN_RESP" | grep -o '"error":"[^"]*"' | head -1 | sed 's/.*:"//;s/"$//')
    fi

    if [ -n "$GITHUB_TOKEN" ]; then
      echo "  Authorized!"
      break
    fi

    case "$TOKEN_ERROR" in
      authorization_pending) continue ;;
      slow_down) POLL_INTERVAL=$((POLL_INTERVAL + 5)) ;;
      *)
        echo "  Authorization failed: $TOKEN_ERROR"
        exit 1
        ;;
    esac
  done

  if [ -z "$GITHUB_TOKEN" ]; then
    echo "  Timed out waiting for authorization."
    exit 1
  fi

  # Accept pending repo collaboration invitations
  echo ""
  echo "  Accepting repository invitations..."
  INVITATIONS=$(curl -s -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \\
    "https://api.github.com/user/repository_invitations" 2>/dev/null || echo "[]")

  if $HAS_JQ; then
    echo "$INVITATIONS" | jq -r --arg org "$GITHUB_ORG" \\
      '.[] | select(.repository.owner.login == $org) | .id' 2>/dev/null | while read -r INV_ID; do
      curl -s -X PATCH -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \\
        "https://api.github.com/user/repository_invitations/$INV_ID" >/dev/null 2>&1
      echo "  Accepted invitation #$INV_ID"
    done
  fi
fi

DIR_SLUG=$(echo "$GITHUB_ORG" | tr '[:upper:]' '[:lower:]')
EGREGORE_DIR="./egregore-$DIR_SLUG"
MEMORY_DIR_NAME=$(basename "$MEMORY_URL" .git)
MEMORY_DIR="./$MEMORY_DIR_NAME"

# Configure git credentials for HTTPS cloning
git config credential.helper store 2>/dev/null || true
printf 'protocol=https\\nhost=github.com\\nusername=x-access-token\\npassword=%s\\n' "$GITHUB_TOKEN" | git credential-store store 2>/dev/null || true

# Embed token in URLs for private repos (credential helper may not work)
AUTHED_FORK=$(echo "$FORK_URL" | sed "s|https://github.com/|https://x-access-token:$GITHUB_TOKEN@github.com/|")
AUTHED_MEMORY=$(echo "$MEMORY_URL" | sed "s|https://github.com/|https://x-access-token:$GITHUB_TOKEN@github.com/|")

# Clone fork
echo "  [2/5] Cloning egregore..."
if [ -d "$EGREGORE_DIR" ]; then
  echo "         Already exists — pulling latest"
  git -C "$EGREGORE_DIR" pull -q
else
  git clone -q "$AUTHED_FORK" "$EGREGORE_DIR"
  git -C "$EGREGORE_DIR" remote set-url origin "$FORK_URL" 2>/dev/null || true
fi

# Clone memory
echo "  [3/5] Cloning shared memory..."
if [ -d "$MEMORY_DIR" ]; then
  echo "         Already exists — pulling latest"
  git -C "$MEMORY_DIR" pull -q
else
  git clone -q "$AUTHED_MEMORY" "$MEMORY_DIR"
  git -C "$MEMORY_DIR" remote set-url origin "$MEMORY_URL" 2>/dev/null || true
fi

# Symlink
echo "  [4/5] Linking memory..."
if [ ! -L "$EGREGORE_DIR/memory" ]; then
  ln -s "../$MEMORY_DIR_NAME" "$EGREGORE_DIR/memory"
fi

# Write .env (secrets only — never committed to git)
echo "  [5/5] Writing credentials..."
cat > "$EGREGORE_DIR/.env" << ENVEOF
GITHUB_TOKEN=$GITHUB_TOKEN
EGREGORE_API_KEY=$API_KEY
ENVEOF
chmod 600 "$EGREGORE_DIR/.env"

# Clone managed repos (if any)
REPO_LIST=""
if $HAS_JQ; then
  REPO_LIST=$(echo "$RESPONSE" | jq -r '.repos[]? // empty' 2>/dev/null)
fi
STEP=6
REPO_DIRS=""
for REPO in $REPO_LIST; do
  echo "  [$STEP] Cloning $REPO..."
  REPO_DIR="./$REPO"
  if [ -d "$REPO_DIR" ]; then
    echo "         Already exists — pulling latest"
    git -C "$REPO_DIR" pull -q
  else
    git clone -q "https://github.com/$GITHUB_ORG/$REPO.git" "$REPO_DIR"
  fi
  REPO_DIRS="$REPO_DIRS $REPO_DIR"
  STEP=$((STEP + 1))
done

# Register instance
mkdir -p "$HOME/.egregore"
REGISTRY="$HOME/.egregore/instances.json"
if [ ! -f "$REGISTRY" ]; then
  echo "[]" > "$REGISTRY"
fi
if $HAS_JQ; then
  ENTRY=$(jq -n --arg s "$SLUG" --arg n "$ORG_NAME" --arg p "$(cd "$EGREGORE_DIR" && pwd)" \\
    '{{slug: $s, name: $n, path: $p}}')
  jq --argjson e "$ENTRY" '(map(select(.path != ($e.path)))) + [$e]' "$REGISTRY" > "$REGISTRY.tmp" \\
    && mv "$REGISTRY.tmp" "$REGISTRY"
fi

# Install egregore alias (uses script from cloned repo)
ALIAS_NAME=$(bash "$EGREGORE_DIR/bin/ensure-shell-function.sh" 2>/dev/null | cut -d: -f1 || echo "egregore")

echo ""
echo "  Egregore is ready for $ORG_NAME"
echo ""
echo "  Your workspace:"
echo "    $EGREGORE_DIR/   — Your Egregore instance"
echo "    $MEMORY_DIR/     — Shared knowledge"
for REPO_DIR in $REPO_DIRS; do
  echo "    $REPO_DIR/       — Managed repo"
done
if [ -n "$TELEGRAM_LINK" ]; then
  echo ""
  echo "  Join the Telegram group for notifications:"
  echo "    $TELEGRAM_LINK"
fi
echo ""
echo "  Next: open a new terminal and type $ALIAS_NAME to start."
echo ""
"""
    return PlainTextResponse(script, media_type="text/plain")


@app.post("/api/org/telegram")
async def org_telegram(body: OrgTelegram, authorization: str = Header(None)):
    """Bot reports its chat_id after being added to a group."""
    # Authenticate: accept bot token or dedicated bot secret
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    bot_secret = os.environ.get("TELEGRAM_BOT_SECRET", "")
    if authorization:
        auth_value = authorization.replace("Bearer ", "").strip()
        valid = (
            (bot_secret and secrets.compare_digest(auth_value, bot_secret))
            or (bot_token and secrets.compare_digest(auth_value, bot_token))
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid authorization")
    elif bot_secret or bot_token:
        raise HTTPException(status_code=401, detail="Missing authorization")

    slug = body.org_slug
    org = ORG_CONFIGS.get(slug)
    if not org:
        raise HTTPException(status_code=404, detail=f"Unknown org: {slug}")

    # Persist to Supabase first (if enabled), then Neo4j, then in-memory
    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            update_fields = {"telegram_chat_id": body.chat_id}
            if body.group_title:
                update_fields["telegram_group_title"] = body.group_title
            if body.group_username:
                update_fields["telegram_group_username"] = body.group_username
            sb.update_org(slug, **update_fields)
            sb.log_telegram_event(slug, "group_connected", chat_id=body.chat_id, group_title=body.group_title)
            logger.info(f"Supabase telegram config updated for {slug}")
        except Exception as e:
            logger.error(f"Supabase telegram persist failed: {e}")
            raise HTTPException(status_code=503, detail="Failed to persist telegram config. Please retry.")

    try:
        neo4j_params = {"chat_id": body.chat_id}
        set_clauses = ["o.telegram_chat_id = $chat_id"]
        if body.group_title:
            neo4j_params["group_title"] = body.group_title
            set_clauses.append("o.telegram_group_title = $group_title")
        if body.group_username:
            neo4j_params["group_username"] = body.group_username
            set_clauses.append("o.telegram_group_username = $group_username")

        neo4j_result = await execute_query(org, f"""
            MATCH (o:Org {{id: $_org}})
            SET {', '.join(set_clauses)}
        """, neo4j_params)
        if isinstance(neo4j_result, dict) and "error" in neo4j_result:
            if not USE_SUPABASE:
                raise HTTPException(status_code=503, detail="Failed to persist telegram config. Please retry.")
            logger.warning(f"Neo4j telegram persist failed (non-fatal with Supabase): {neo4j_result}")
    except HTTPException:
        raise
    except Exception as e:
        if not USE_SUPABASE:
            logger.error(f"Failed to persist telegram config: {e}")
            raise HTTPException(status_code=503, detail="Failed to persist telegram config. Please retry.")
        logger.warning(f"Neo4j telegram persist failed (non-fatal with Supabase): {e}")

    # Update in-memory after persistence
    ORG_CONFIGS[slug]["telegram_chat_id"] = body.chat_id
    if body.group_title:
        ORG_CONFIGS[slug]["telegram_group_title"] = body.group_title
    if body.group_username:
        ORG_CONFIGS[slug]["telegram_group_username"] = body.group_username

    return {
        "status": "connected",
        "org_slug": slug,
        "org_name": org.get("org_name", slug),
        "group_title": body.group_title,
        "group_username": body.group_username,
    }


@app.get("/api/org/telegram/status/{slug}")
async def org_telegram_status(slug: str):
    """Check if Telegram is connected for an org (for polling from website)."""
    org = ORG_CONFIGS.get(slug)
    if not org:
        raise HTTPException(status_code=404, detail=f"Unknown org: {slug}")

    connected = bool(org.get("telegram_chat_id"))
    return {
        "connected": connected,
        "org_slug": slug,
        "group_title": org.get("telegram_group_title"),
        "group_username": org.get("telegram_group_username"),
    }


@app.post("/api/org/telegram/invite-link")
async def org_telegram_invite_link(org: dict = Depends(validate_api_key)):
    """Generate a one-time Telegram group invite link for the org."""
    link = await create_group_invite_link(org)
    if not link:
        raise HTTPException(status_code=400, detail="Telegram not configured or bot lacks permission")
    return {"invite_link": link}


@app.get("/api/org/{slug}/telegram/membership")
async def org_telegram_membership(slug: str, authorization: str = Header(...)):
    """Check if a user is in the org's Telegram group.

    Auth: GitHub token (from setup flow OAuth).
    Looks up Person by GitHub username → gets telegramId → checks TelegramUser IN_GROUP.
    """
    import httpx

    github_token = authorization.replace("Bearer ", "").strip()
    if not github_token:
        raise HTTPException(status_code=401, detail="Missing GitHub token")

    org_config = ORG_CONFIGS.get(slug)
    if not org_config:
        raise HTTPException(status_code=404, detail="Org not found")
    org = {**org_config, "slug": slug}

    # Get GitHub username
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )
    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    username = user_resp.json().get("login", "")

    # Check if Telegram is configured for this org
    if not org.get("telegram_chat_id"):
        return {"status": "not_configured", "in_group": False, "group_name": None}

    # Get actual Telegram group title from Telegram API
    bot_token = org.get("telegram_bot_token", "")
    chat_id = org.get("telegram_chat_id", "")
    group_name = slug  # fallback
    async with httpx.AsyncClient() as client:
        try:
            chat_resp = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                params={"chat_id": chat_id},
                timeout=10.0,
            )
            chat_data = chat_resp.json()
            if chat_data.get("ok"):
                group_name = chat_data["result"].get("title", slug)
        except Exception:
            pass

    # Cross-org query: uses execute_system_query to bypass org scoping
    # (TelegramUser → Person lookup spans all orgs intentionally)
    membership_result = await execute_system_query(org, """
        MATCH (tu:TelegramUser)-[:IDENTIFIES]->(p)
        WHERE p.github = $username OR p.name = $username OR p.name = $usernameLower
        WITH DISTINCT tu
        OPTIONAL MATCH (tu)-[r:IN_GROUP {status: 'active'}]->(o:Org {id: $orgId})
        RETURN count(r) AS cnt, tu.username AS telegramUsername
    """, {"username": username, "usernameLower": username.lower(), "orgId": slug})

    membership_values = membership_result.get("values", [])
    in_group = bool(membership_values and membership_values[0] and membership_values[0][0] > 0)
    telegram_username = membership_values[0][1] if membership_values and len(membership_values[0]) > 1 else None

    # Generate invite link so frontend can link to the group
    telegram_group_link = None
    try:
        telegram_group_link = await create_group_invite_link(org)
    except Exception:
        pass

    return {
        "status": "configured",
        "in_group": in_group,
        "group_name": group_name,
        "telegram_group_link": telegram_group_link,
        "telegram_username": telegram_username,
    }


@app.get("/api/auth/github/client-id")
async def github_client_id():
    """Return the GitHub OAuth client ID for the web flow."""
    return {"client_id": GITHUB_CLIENT_ID}


# =============================================================================
# USER PROFILE
# =============================================================================


async def _get_org_api_key(org_config: dict, slug: str) -> str:
    """Retrieve the org's plaintext API key.

    Priority: in-memory → Supabase key_plaintext → Neo4j Org node (legacy).
    """
    # 1. Check in-memory config
    key = org_config.get("api_key", "")
    if key:
        return key

    # 2. Read plaintext from Supabase (primary persistent store)
    if USE_SUPABASE:
        try:
            from .services.supabase import get_active_api_key_plaintext
            key = get_active_api_key_plaintext(slug)
            if key:
                org_config["api_key"] = key
                if slug in ORG_CONFIGS:
                    ORG_CONFIGS[slug]["api_key"] = key
                return key
        except Exception as e:
            logger.warning(f"Failed to retrieve API key from Supabase for {slug}: {e}")

    # 3. Fallback: Neo4j Org node (legacy, for keys created before Supabase plaintext storage)
    try:
        result = await execute_system_query(org_config, """
            MATCH (o:Org {id: $slug})
            RETURN o.api_key AS api_key
        """, {"slug": slug})
        values = result.get("values", [])
        if values and values[0] and values[0][0]:
            key = values[0][0]
            org_config["api_key"] = key
            if slug in ORG_CONFIGS:
                ORG_CONFIGS[slug]["api_key"] = key
            return key
    except Exception as e:
        logger.warning(f"Failed to retrieve API key from Neo4j for {slug}: {e}")

    return ""


def _get_seed_org() -> dict | None:
    """Get a seed org config with Neo4j access for cross-org queries.

    Prefers the shared customer database (EGREGORE_NEO4J_HOST) since cross-org
    queries are used by the web setup flow, which is customer-facing.
    """
    host = os.environ.get("EGREGORE_NEO4J_HOST", "")
    if host:
        return {
            "neo4j_host": host,
            "neo4j_user": os.environ.get("EGREGORE_NEO4J_USER", "neo4j"),
            "neo4j_password": os.environ.get("EGREGORE_NEO4J_PASSWORD", ""),
            "slug": "__system__",
        }
    for slug, org in ORG_CONFIGS.items():
        if org.get("neo4j_host"):
            return {**org, "slug": slug}
    return None


@app.get("/api/user/profile")
async def user_profile_get(authorization: str = Header(...)):
    """Get user profile: Telegram handle + org memberships.

    Auth: GitHub token. Cross-org lookup (not scoped to a single org).
    """
    import httpx

    github_token = authorization.replace("Bearer ", "").strip()
    if not github_token:
        raise HTTPException(status_code=401, detail="Missing GitHub token")

    # Get GitHub user info
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )
    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    github_user = user_resp.json()
    username = github_user.get("login", "")
    name = github_user.get("name", "") or username

    seed_org = _get_seed_org()
    if not seed_org:
        return {
            "github_username": username,
            "name": name,
            "telegram_username": None,
            "memberships": [],
        }

    # Cross-org queries use execute_system_query (bypasses org scoping)
    # Query 1: Find TelegramUser linked to any Person with this github
    tu_result = await execute_system_query(seed_org, """
        MATCH (tu:TelegramUser)-[:IDENTIFIES]->(p)
        WHERE p.github = $username
        RETURN tu.username AS tuUser, COLLECT(DISTINCT p.org) AS orgs
    """, {"username": username})

    tu_username = None
    org_ids = set()

    tu_values = tu_result.get("values", [])
    if tu_values and tu_values[0]:
        tu_username = tu_values[0][0]
        org_ids.update(o for o in (tu_values[0][1] or []) if o)

    # Query 2 (fallback): Find unlinked Person nodes
    p_result = await execute_system_query(seed_org, """
        MATCH (p) WHERE p.github = $username AND p.org IS NOT NULL
        RETURN COLLECT(DISTINCT p.org) AS orgs
    """, {"username": username})

    p_values = p_result.get("values", [])
    if p_values and p_values[0]:
        org_ids.update(o for o in (p_values[0][0] or []) if o)

    # Query 3: For each org, get Org name + IN_GROUP status
    memberships = []
    if org_ids:
        org_result = await execute_system_query(seed_org, """
            MATCH (o:Org) WHERE o.id IN $orgIds
            OPTIONAL MATCH (tu:TelegramUser {username: $tuUser})-[r:IN_GROUP {status: 'active'}]->(o)
            RETURN o.id AS slug, o.name AS name, count(r) > 0 AS inGroup
        """, {"orgIds": list(org_ids), "tuUser": tu_username or ""})

        for row in org_result.get("values", []):
            if row and len(row) >= 3:
                memberships.append({
                    "org_slug": row[0],
                    "org_name": row[1] or row[0],
                    "in_telegram_group": bool(row[2]),
                })

    return {
        "github_username": username,
        "name": name,
        "telegram_username": tu_username,
        "memberships": memberships,
    }


@app.post("/api/user/profile")
async def user_profile_update(body: UserProfileUpdate, authorization: str = Header(...)):
    """Update user profile: set Telegram handle and/or display name.

    Auth: GitHub token. Cross-org update.
    """
    import httpx

    github_token = authorization.replace("Bearer ", "").strip()
    if not github_token:
        raise HTTPException(status_code=401, detail="Missing GitHub token")

    if not body.telegram_username and not body.display_name:
        raise HTTPException(status_code=400, detail="Must provide telegram_username or display_name")

    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )
    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    github_user = user_resp.json()
    username = github_user.get("login", "")
    name = github_user.get("name", "") or username

    seed_org = _get_seed_org()
    if not seed_org:
        raise HTTPException(status_code=503, detail="No Neo4j connection available")

    tg_handle = None
    if body.telegram_username:
        # Strip leading @ and trim
        tg_handle = body.telegram_username.lstrip("@").strip()
        if not tg_handle:
            raise HTTPException(status_code=400, detail="Telegram username cannot be empty")

        # Cross-org queries use execute_system_query (bypasses org scoping)
        # Set telegramUsername on all Person nodes with matching github
        await execute_system_query(seed_org, """
            MATCH (p) WHERE p.github = $username
            SET p.telegramUsername = $tgHandle
        """, {"username": username, "tgHandle": tg_handle})

        # MERGE TelegramUser and IDENTIFIES relationships
        await execute_system_query(seed_org, """
            MERGE (tu:TelegramUser {username: $tgHandle})
            WITH tu
            MATCH (p) WHERE p.github = $username
            MERGE (tu)-[:IDENTIFIES]->(p)
        """, {"username": username, "tgHandle": tg_handle})

    # Note: display_name is NOT updated here. It's a per-org setting — each Egregore
    # instance sets it via /me → bin/graph.sh (org-scoped) → /api/user/ensure (org-scoped).
    # This endpoint is cross-org (GitHub-token auth), so it only handles truly global
    # properties like telegram_username.

    # Return updated profile (same shape as GET)
    # Re-gather memberships
    tg_for_query = tg_handle or ""
    org_result = await execute_system_query(seed_org, """
        MATCH (p) WHERE p.github = $username AND p.org IS NOT NULL
        WITH COLLECT(DISTINCT p.org) AS orgIds
        UNWIND orgIds AS oid
        MATCH (o:Org {id: oid})
        OPTIONAL MATCH (tu:TelegramUser {username: $tgHandle})-[r:IN_GROUP {status: 'active'}]->(o)
        WHERE $tgHandle <> ''
        RETURN o.id AS slug, o.name AS name, count(r) > 0 AS inGroup
    """, {"username": username, "tgHandle": tg_for_query})

    memberships = []
    for row in org_result.get("values", []):
        if row and len(row) >= 3:
            memberships.append({
                "org_slug": row[0],
                "org_name": row[1] or row[0],
                "in_telegram_group": bool(row[2]),
            })

    return {
        "github_username": username,
        "name": name,
        "display_name": body.display_name,
        "telegram_username": tg_handle,
        "memberships": memberships,
    }


# =============================================================================
# INVITE FLOW
# =============================================================================


@app.post("/api/org/invite")
async def org_invite(body: OrgInvite, authorization: str = Header(...)):
    """Invite a GitHub user to an org's Egregore.

    Auth modes:
    - API key in header (preferred): Authorization: Bearer ek_<slug>_<secret>
      GitHub token passed in body.github_token for org operations.
    - GitHub token in header (legacy): Authorization: Bearer ghp_<token>
      Falls back to slug resolution cascade.

    The inviter must be an admin of the GitHub org.
    Sends a GitHub org invitation + creates an Egregore invite link.
    """
    auth_value = authorization.replace("Bearer ", "").strip()

    # Determine auth mode: API key (ek_) or legacy GitHub token (ghp_ / gho_ / github_pat_)
    if auth_value.startswith("ek_"):
        # New path: API key identifies the Egregore instance
        from .auth import get_org_slug
        slug = get_org_slug(auth_value)

        # Validate the API key is real
        org_config = ORG_CONFIGS.get(slug)
        if not org_config or not secrets.compare_digest(org_config.get("api_key", ""), auth_value):
            # Try Supabase validation
            if USE_SUPABASE:
                try:
                    from .services.supabase import validate_api_key as sb_validate
                    org_row = sb_validate(auth_value)
                    if not org_row:
                        raise HTTPException(status_code=401, detail="Invalid API key")
                    org_name = org_row.get("name", body.github_org)
                except Exception:
                    raise HTTPException(status_code=401, detail="Invalid API key")
            else:
                raise HTTPException(status_code=401, detail="Invalid API key")
        else:
            org_name = org_config.get("org_name", body.github_org)

        # GitHub token must be in the body
        token = body.github_token
        if not token:
            raise HTTPException(
                status_code=400,
                detail="github_token required in body when using API key auth",
            )
    else:
        # Legacy path: GitHub token in header, slug resolved via cascade
        token = auth_value
        slug = None
        org_name = body.github_org

    try:
        inviter = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    owner = body.github_org

    # Verify the repo exists (org has Egregore)
    if not await gh.repo_exists(token, owner, body.repo_name):
        raise HTTPException(status_code=404, detail=f"No Egregore setup found for {owner} ({body.repo_name})")

    # Detect personal vs org account
    is_personal = not await gh.is_org(token, owner)

    if is_personal:
        # Personal account: inviter must be the repo owner
        if inviter["login"].lower() != owner.lower():
            raise HTTPException(status_code=403, detail="Only the account owner can invite.")
    else:
        # Org account: inviter must be admin
        role = await gh.get_org_membership(token, owner)
        if role not in ("admin",):
            raise HTTPException(
                status_code=403,
                detail="Only org admins can invite. Ask an admin to send the invite.",
            )

    # Add as collaborator on the egregore repo (works for both personal and org accounts)
    collab_ok = await gh.add_repo_collaborator(token, owner, body.repo_name, body.github_username)
    if collab_ok:
        github_result = {"status": "collaborator_invited"}
    else:
        logger.warning(f"Failed to add {body.github_username} as collaborator to {owner}/{body.repo_name}")
        github_result = {"status": "collaborator_failed", "reason": "Check token scopes (needs 'repo')"}

    # --- Slug resolution (only needed for legacy auth path) ---
    config = {}
    repos = []
    memory_repo = f"{owner}-memory"

    if not slug:
        slug = body.slug or None

        # 1. If slug provided, look it up directly
        if slug and slug in ORG_CONFIGS:
            org_name = ORG_CONFIGS[slug].get("org_name", owner)
        elif not slug:
            # 1b. No slug — try matching by github_org, but ONLY if unambiguous.
            matching = [(s, c) for s, c in ORG_CONFIGS.items()
                         if c.get("github_org", "").lower() == owner.lower()]
            if len(matching) == 1:
                slug = matching[0][0]
                org_name = matching[0][1].get("org_name", owner)

        # 2. Read egregore.json from the repo
        if not slug:
            config_raw = await gh.get_file_content(token, owner, body.repo_name, "egregore.json")
            if config_raw:
                config = json.loads(config_raw)
                org_name = config.get("org_name", owner)
                slug = config.get("slug")

        # 3. Fall back to Supabase lookup
        if not slug and USE_SUPABASE:
            try:
                from .services.supabase import get_client
                result = get_client().table("orgs").select("slug, name, github_org").eq("github_org", owner).execute()
                if result.data and len(result.data) == 1:
                    row = result.data[0]
                    slug = row["slug"]
                    org_name = row.get("name", owner)
            except Exception as e:
                logger.warning(f"Supabase org lookup failed for {owner}: {e}")

    if not slug:
        raise HTTPException(
            status_code=400,
            detail="Could not resolve org slug. Org setup may be incomplete.",
        )

    # Read repo config for memory_repo and repos (best-effort from egregore.json)
    if not config:
        config_raw = await gh.get_file_content(token, owner, body.repo_name, "egregore.json")
        if config_raw:
            config = json.loads(config_raw)
    repos = config.get("repos", [])
    memory_repo = config.get("memory_repo", f"{owner}-memory")
    if "/" in memory_repo:
        memory_repo_name = memory_repo.split("/")[-1].replace(".git", "")
    else:
        memory_repo_name = memory_repo
    mem_ok = await gh.add_repo_collaborator(token, owner, memory_repo_name, body.github_username)
    if not mem_ok:
        logger.warning(f"Failed to add {body.github_username} as collaborator to {owner}/{memory_repo_name}")

    # Add as collaborator on managed repos
    for repo_name in repos:
        repo_ok = await gh.add_repo_collaborator(token, owner, repo_name, body.github_username)
        if not repo_ok:
            logger.warning(f"Failed to add {body.github_username} as collaborator to {owner}/{repo_name}")

    # Create invite token (7-day TTL)
    site_url = os.environ.get("EGREGORE_SITE_URL", "https://egregore-core.netlify.app")
    invite_token = create_invite_token({
        "github_org": owner,
        "org_name": org_name,
        "invited_username": body.github_username,
        "invited_by": inviter["login"],
        "slug": slug,
        "repos": repos,
        "repo_name": body.repo_name,
        "is_personal": is_personal,
    })

    invite_url = f"{site_url}/join?invite={invite_token}"

    logger.info(f"Invite created: {inviter['login']} invited {body.github_username} to {owner}")

    return {
        "invite_url": invite_url,
        "invite_token": invite_token,
        "github_invite": github_result,
        "org_name": org_name,
        "invited_username": body.github_username,
    }


@app.get("/api/org/invite/{token}")
async def org_invite_info(token: str):
    """Get invite details without consuming the token. For the website to show invite info."""
    data = peek_token(token)
    if not data:
        raise HTTPException(status_code=404, detail="Invite expired or invalid")
    return {
        "org_name": data.get("org_name", ""),
        "github_org": data.get("github_org", ""),
        "invited_by": data.get("invited_by", ""),
        "invited_username": data.get("invited_username", ""),
    }


@app.post("/api/org/invite/{invite_token}/accept")
async def org_invite_accept(invite_token: str, authorization: str = Header(...)):
    """Accept an invite. Verifies invitee identity, registers them, returns setup token.

    The invitee's GitHub token (repo,read:org scope) is passed through to the setup token
    so the CLI can clone repos without a separate device flow auth.
    """
    token = authorization.replace("Bearer ", "").strip()

    # Validate invite token (peek, don't consume yet)
    invite_data = peek_token(invite_token)
    if not invite_data:
        raise HTTPException(status_code=404, detail="Invite expired or invalid")

    # Verify invitee identity (works with read:user scope)
    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    owner = invite_data["github_org"]
    slug = invite_data.get("slug")
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="Invite token is missing 'slug'. It may have been created before slug was added to invites. Ask the admin to send a new invite.",
        )

    # Build config from invite data — invitee may not have repo access yet (expected)
    invite_repo_name = invite_data.get("repo_name", "")
    repos = invite_data.get("repos", [])

    # Try to read egregore.json from repo (may fail if invitee hasn't accepted collab invite yet)
    config = None
    try:
        config_raw = await gh.get_file_content(token, owner, invite_repo_name, "egregore.json")
        if config_raw:
            config = json.loads(config_raw)
    except Exception:
        pass

    # Build URLs — fall back to invite data if we can't read the repo
    if config:
        memory_repo = config.get("memory_repo", f"{owner}-memory")
        api_url = config.get("api_url", "")
        org_name = config.get("org_name", owner)
        repos = config.get("repos", repos)
    else:
        memory_repo = invite_data.get("memory_repo", f"{owner}-memory")
        api_url = ""
        org_name = invite_data.get("org_name", owner)

    if isinstance(memory_repo, str) and memory_repo.startswith("http"):
        memory_url = memory_repo
    else:
        memory_url = f"https://github.com/{owner}/{memory_repo}.git"

    fork_url = f"https://github.com/{owner}/{invite_repo_name}.git"

    org_config = ORG_CONFIGS.get(slug)

    # Register user + membership in Supabase immediately (don't defer to session-start)
    if USE_SUPABASE:
        try:
            from .services.supabase import upsert_user, add_membership
            upsert_user(
                github_username=user["login"],
                github_name=user.get("name"),
                avatar_url=user.get("avatar_url"),
            )
            invited_by = invite_data.get("invited_by")
            add_membership(slug, user["login"], role="member", invited_by_username=invited_by)
            logger.info(f"Invite accept: registered {user['login']} in Supabase for {slug}")
        except Exception as e:
            logger.warning(f"Invite accept: Supabase registration failed for {user['login']}: {e}")

    # Consume the invite token now
    claim_token(invite_token)

    # Get API key from server config (not from egregore.json — secrets don't go in git)
    api_key = await _get_org_api_key(org_config, slug) if org_config else ""

    # Pass the user's GitHub token from website OAuth (has repo,read:org scope)
    setup_token = create_token({
        "fork_url": fork_url,
        "memory_url": memory_url,
        "github_token": token,
        "api_key": api_key,
        "api_url": api_url,
        "org_name": org_name,
        "github_org": owner,
        "github_username": user["login"],
        "github_name": user.get("name", user["login"]),
        "slug": slug,
        "repos": repos,
        "repo_name": invite_repo_name,
    })

    # Generate Telegram group invite link for the new member
    telegram_group_link = None
    if org_config:
        telegram_group_link = await create_group_invite_link(org_config)

    # If org has hosted Coder, pre-create user + workspace (fire-and-forget — don't block invite accept)
    coder_url = None
    try:
        coder_url, coder_token = await _get_coder_credentials(slug)
        if coder_url and coder_token:
            from .services.coder import CoderClient
            from .services import supabase as sb
            coder_client = CoderClient(coder_url, coder_token)
            github_username = user["login"]
            # Get org data for workspace params
            org_row = sb.get_client().table("orgs").select(
                "name, github_org, repo_name, managed_repos"
            ).eq("slug", slug).execute()
            org_data = org_row.data[0] if org_row.data else {}
            # Create Coder user
            coder_result = await coder_client.create_user(
                username=github_username,
                email=f"{github_username}@users.noreply.github.com",
                name=user.get("name", github_username),
            )
            logger.info(f"Coder user for invite: {github_username} → {coder_result.get('status')}")
            # Create workspace with org params
            ws_result = await coder_client.create_workspace(
                owner=github_username,
                org_slug=slug,
                org_name=org_data.get("name", slug),
                github_org=org_data.get("github_org", ""),
                repo_name=org_data.get("repo_name", ""),
                managed_repos=org_data.get("managed_repos", ""),
            )
            logger.info(f"Coder workspace for invite: {github_username} → {ws_result.get('status')}")
            # Store coder_username on membership so terminal URL works
            try:
                sb.get_client().table("memberships").update(
                    {"coder_username": github_username}
                ).eq("org_slug", slug).eq(
                    "user_id", sb.get_user_by_github(github_username)["id"]
                ).execute()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to create Coder user/workspace for {user['login']}: {e}")

    return {
        "status": "accepted",
        "setup_token": setup_token,
        "fork_url": fork_url,
        "memory_url": memory_url,
        "org_slug": slug,
        "org_name": org_name,
        "telegram_group_link": telegram_group_link,
        "hosting_coder_url": coder_url,
    }


# =============================================================================
# NEW SUPABASE-BACKED ENDPOINTS
# =============================================================================


@app.get("/api/user/orgs")
async def user_orgs(authorization: str = Header(...)):
    """Get all orgs a user belongs to.

    Auth: GitHub token. Returns orgs with membership details.
    """
    import httpx

    github_token = authorization.replace("Bearer ", "").strip()
    if not github_token:
        raise HTTPException(status_code=401, detail="Missing GitHub token")

    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {github_token}"},
            timeout=10.0,
        )
    if user_resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    username = user_resp.json().get("login", "")

    if USE_SUPABASE:
        from .services import supabase as sb
        memberships = sb.get_user_orgs(username)
        return {
            "github_username": username,
            "orgs": [
                {
                    "slug": m["orgs"]["slug"],
                    "name": m["orgs"]["name"],
                    "github_org": m["orgs"]["github_org"],
                    "role": m["role"],
                    "has_telegram": bool(m["orgs"].get("telegram_chat_id")),
                }
                for m in memberships
                if m.get("orgs")
            ],
        }

    # Fallback: derive from ORG_CONFIGS (limited info)
    return {
        "github_username": username,
        "orgs": [
            {"slug": slug, "name": cfg.get("org_name", slug), "role": "member"}
            for slug, cfg in ORG_CONFIGS.items()
        ],
    }


@app.get("/api/org/{slug}/members")
async def org_members(slug: str, org: dict = Depends(validate_api_key)):
    """Get all members of an org.

    Auth: API key. Returns member list with roles.
    """
    if org.get("slug") != slug:
        raise HTTPException(status_code=403, detail="API key does not match org")

    if USE_SUPABASE:
        from .services import supabase as sb
        members = sb.get_memberships(slug)
        return {
            "org_slug": slug,
            "members": [
                {
                    "github_username": m["users"]["github_username"] if m.get("users") else None,
                    "github_name": m["users"]["github_name"] if m.get("users") else None,
                    "display_name": m.get("display_name"),
                    "role": m["role"],
                    "status": m["status"],
                    "joined_at": m.get("joined_at"),
                }
                for m in members
            ],
        }

    return {"org_slug": slug, "members": []}


@app.delete("/api/org/{slug}/members/{username}", response_model=RemoveMemberResponse)
async def remove_member(
    slug: str,
    username: str,
    mode: str = Query("revoke", pattern="^(revoke|full)$"),
    authorization: str = Header(...),
):
    """Remove a member from an org.

    Auth: GitHub token. Caller must be org admin (Supabase membership) or platform admin (ADMIN_USERS).
    Modes:
      - revoke: kill access, keep contributions (Person node marked status='removed')
      - full: revoke + erase data from Neo4j and memory
    """
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Member management requires Supabase")

    # Validate caller's GitHub token
    github_username = await validate_github_token(authorization)
    github_token = authorization.replace("Bearer ", "").strip()

    from .services import supabase as sb

    # --- Auth: caller must be org admin or platform admin ---
    is_platform_admin = github_username.lower() in {u.lower() for u in ADMIN_USERS}
    caller_membership = sb.get_membership_by_username(slug, github_username)
    is_org_admin = caller_membership and caller_membership.get("role") == "admin" and caller_membership.get("status") == "active"

    if not is_platform_admin and not is_org_admin:
        raise HTTPException(status_code=403, detail="Only org admins or platform admins can remove members")

    # --- Validate target ---
    target_membership = sb.get_membership_by_username(slug, username)
    graph_only = False

    if not target_membership or target_membership.get("status") != "active":
        # Check if the user exists as a graph-only Person node (no Supabase membership)
        org_config = ORG_CONFIGS.get(slug)
        if org_config and mode == "full":
            try:
                result = await execute_system_query(org_config, """
                    MATCH (p:Person) WHERE p.github = $username OR p.name = $username RETURN p.name AS name
                """, {"username": username})
                has_node = bool(result.get("values") and result["values"][0][0])
            except Exception:
                has_node = False
            if has_node:
                graph_only = True
            else:
                raise HTTPException(status_code=404, detail=f"No active membership or graph node found for '{username}' in org '{slug}'")
        else:
            raise HTTPException(status_code=404, detail=f"No active membership found for '{username}' in org '{slug}'")

    # --- Cannot remove an admin ---
    if target_membership and target_membership.get("role") == "admin":
        raise HTTPException(status_code=400, detail="Cannot remove an admin. Change their role first.")

    # --- Cannot remove yourself ---
    if username.lower() == github_username.lower():
        raise HTTPException(status_code=400, detail="Cannot remove yourself")

    actions = []
    errors = []
    org_config = ORG_CONFIGS.get(slug)

    if not graph_only:
        # --- Step 1: Revoke GitHub access ---
        try:
            org_data = sb.get_org_by_slug(slug)
            if org_data:
                github_org = org_data.get("github_org", "")
                if github_org:
                    repos_to_remove = ["egregore-core", f"{slug}-memory"]
                    if org_config:
                        for repo_name in org_config.get("repos", []):
                            repos_to_remove.append(repo_name)

                    removed_count = 0
                    for repo in repos_to_remove:
                        try:
                            ok = await gh.remove_repo_collaborator(github_token, github_org, repo, username)
                            if ok:
                                removed_count += 1
                        except Exception:
                            pass
                    actions.append(f"GitHub: removed from {removed_count}/{len(repos_to_remove)} repos")
        except Exception as e:
            errors.append(f"GitHub access revocation failed: {str(e)}")

        # --- Step 2: Deactivate in Supabase ---
        try:
            sb.remove_membership(slug, username)
            actions.append("Supabase: membership status set to 'removed'")
        except Exception as e:
            errors.append(f"Supabase membership removal failed: {str(e)}")
    else:
        actions.append("Graph-only node (no Supabase membership)")

    # --- Step 3: Mode-specific cleanup ---
    if mode == "full":
        # Delete telemetry data
        try:
            deleted = sb.delete_user_telemetry(slug, username)
            actions.append(f"Supabase: deleted {deleted['telemetry_events']} telemetry events, {deleted['health_checkins']} health checkins")
        except Exception as e:
            errors.append(f"Telemetry deletion failed: {str(e)}")

        # Neo4j: delete Person node and authored data
        # Uses execute_system_query to bypass the append-only guard
        if org_config:
            try:
                await execute_system_query(org_config, """
                    MATCH (p:Person)-[:BY]->(s:Session)
                    WHERE p.github = $username OR p.name = $username
                    DETACH DELETE s
                """, {"username": username})
                actions.append("Neo4j: deleted sessions")

                await execute_system_query(org_config, """
                    MATCH (p:Person)-[r:CONTRIBUTED_BY]-()
                    WHERE p.github = $username OR p.name = $username
                    DELETE r
                """, {"username": username})
                actions.append("Neo4j: removed contribution relationships")

                await execute_system_query(org_config, """
                    MATCH (p:Person)-[r:STARTED_BY]-()
                    WHERE p.github = $username OR p.name = $username
                    DELETE r
                """, {"username": username})
                actions.append("Neo4j: removed quest ownership")

                await execute_system_query(org_config, """
                    MATCH (p:Person)-[:BY]->(t:Todo)
                    WHERE p.github = $username OR p.name = $username
                    DETACH DELETE t
                """, {"username": username})
                actions.append("Neo4j: deleted todos")

                await execute_system_query(org_config, """
                    MATCH (p:Person)-[r:ASKED_BY]->(q)
                    WHERE p.github = $username OR p.name = $username
                    DETACH DELETE q
                """, {"username": username})

                await execute_system_query(org_config, """
                    MATCH (p:Person)
                    WHERE p.github = $username OR p.name = $username
                    DETACH DELETE p
                """, {"username": username})
                actions.append("Neo4j: deleted Person node")
            except Exception as e:
                errors.append(f"Neo4j cleanup failed: {str(e)}")
    else:
        # mode == "revoke": mark Person node as removed
        if org_config:
            try:
                await execute_system_query(org_config, """
                    MATCH (p:Person)
                    WHERE p.github = $username OR p.name = $username
                    SET p.status = 'removed', p.removedAt = datetime()
                """, {"username": username})
                actions.append("Neo4j: Person node marked as removed")
            except Exception as e:
                errors.append(f"Neo4j status update failed: {str(e)}")

    # --- Step 4: Coder cleanup (if org has hosted workspace) ---
    try:
        coder_url, coder_token = await _get_coder_credentials(slug)
        if coder_url and coder_token:
            from .services.coder import CoderClient
            coder_client = CoderClient(coder_url, coder_token)
            # Delete workspace first (required before deleting user)
            ws_result = await coder_client.delete_workspace(owner=username)
            if ws_result.get("status") == "deleted":
                actions.append("Coder: workspace deleted")
            elif ws_result.get("status") == "not_found":
                actions.append("Coder: no workspace found")
            else:
                errors.append(f"Coder workspace deletion: {ws_result.get('detail', 'unknown error')}")
            # Delete user
            user_result = await coder_client.delete_user(username)
            if user_result.get("status") == "deleted":
                actions.append("Coder: user deleted")
            elif user_result.get("status") == "not_found":
                actions.append("Coder: no user found")
            else:
                errors.append(f"Coder user deletion: {user_result.get('detail', 'unknown error')}")
    except Exception as e:
        errors.append(f"Coder cleanup failed: {str(e)}")

    status = "removed" if not errors else "removed"  # partial success still counts
    return RemoveMemberResponse(
        status=status,
        mode=mode,
        username=username,
        actions=actions,
        errors=errors,
    )


@app.post("/api/admin/waitlist")
async def admin_waitlist_add(body: WaitlistAdd):
    """Add to waitlist. No auth required (public endpoint)."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Waitlist requires Supabase")

    from .services import supabase as sb
    entry = sb.waitlist_add(
        name=body.name,
        email=body.email,
        github_username=body.github_username,
        source=body.source,
    )

    # Send confirmation email via Resend (non-blocking)
    try:
        resend_key = os.environ.get("RESEND_API_KEY", "")
        if resend_key and body.email:
            import resend
            resend.api_key = resend_key
            display_name = body.name or "there"
            resend.Emails.send({
                "from": "Egregore <hello@egregore.xyz>",
                "to": [body.email],
                "subject": "You're in the summoning circle",
                "html": (
                    f"<p>Hey {display_name},</p>"
                    "<p>Welcome to the circle. You're on the Egregore waitlist — "
                    "we're building shared cognition for teams and agents, "
                    "and we'll let you know as soon as it's your turn to join.</p>"
                    "<p>In the meantime, if you're curious about what we're building, "
                    'take a look at <a href="https://egregore.xyz">egregore.xyz</a>.</p>'
                    "<p>Talk soon,<br>Oguzhan & Cem</p>"
                ),
            })
    except Exception:
        pass  # signup succeeds even if email fails

    # Notify admins of new signup via email (non-blocking)
    try:
        resend_key = os.environ.get("RESEND_API_KEY", "")
        if resend_key:
            import resend
            resend.api_key = resend_key
            parts = []
            if body.name:
                parts.append(f"<b>Name:</b> {body.name}")
            if body.email:
                parts.append(f"<b>Email:</b> {body.email}")
            if body.github_username:
                parts.append(f"<b>GitHub:</b> {body.github_username}")
            if body.source:
                parts.append(f"<b>Source:</b> {body.source}")
            resend.Emails.send({
                "from": "Egregore <hello@egregore.xyz>",
                "to": ["oguzhan@curvelabs.eu", "cem@curvelabs.eu"],
                "subject": f"New waitlist signup: {body.name or body.email or 'anonymous'}",
                "html": "<br>".join(parts) if parts else "New signup (no details)",
            })
    except Exception:
        pass  # signup succeeds even if notification fails

    return {"status": "added", "id": entry.get("id")}


@app.get("/api/admin/waitlist")
async def admin_waitlist_list(
    status: str = "pending",
    admin_user: str = Depends(validate_admin_github_token),
):
    """List waitlist entries. Auth: GitHub token (admin users) or API key."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Waitlist requires Supabase")

    from .services import supabase as sb
    entries = sb.waitlist_list(status=status)
    return {"entries": entries}


@app.post("/api/admin/waitlist/approve")
async def admin_waitlist_approve(
    body: WaitlistApprove,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Approve a waitlist entry. Auth: GitHub token (admin users) or API key."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Waitlist requires Supabase")

    from .services import supabase as sb
    result = sb.waitlist_approve(body.waitlist_id, approved_by_username=admin_user)
    if not result:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")
    return {"status": "approved", "entry": result}


@app.get("/api/internal/orgs")
async def internal_orgs(authorization: str = Header(...)):
    """Internal endpoint: list all orgs with their config.

    Used by the Telegram bot to load org configs without Neo4j.
    Auth: Bot token or dedicated bot secret.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    bot_secret = os.environ.get("TELEGRAM_BOT_SECRET", "")
    auth_value = authorization.replace("Bearer ", "").strip()

    valid = (
        (bot_secret and secrets.compare_digest(auth_value, bot_secret))
        or (bot_token and secrets.compare_digest(auth_value, bot_token))
    )
    if not valid:
        # Also accept any valid API key (in-memory or Supabase)
        valid = any(org.get("api_key") == auth_value for org in ORG_CONFIGS.values())
        if not valid and USE_SUPABASE:
            try:
                from .services.supabase import validate_api_key as sb_validate
                valid = sb_validate(auth_value) is not None
            except Exception:
                pass
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid authorization")

    if USE_SUPABASE:
        from .services import supabase as sb
        orgs_list = sb.list_orgs()
        result = []
        for org_row in orgs_list:
            result.append({
                "slug": org_row["slug"],
                "name": org_row["name"],
                "github_org": org_row["github_org"],
                "telegram_chat_id": org_row.get("telegram_chat_id"),
                "telegram_group_title": org_row.get("telegram_group_title"),
                "telegram_group_username": org_row.get("telegram_group_username"),
                "neo4j_host": org_row.get("neo4j_host"),
                "neo4j_user": org_row.get("neo4j_user"),
                "neo4j_password": org_row.get("neo4j_password"),
            })
        return {"orgs": result}

    # Fallback: return from in-memory ORG_CONFIGS
    result = []
    for slug, cfg in ORG_CONFIGS.items():
        result.append({
            "slug": slug,
            "name": cfg.get("org_name", slug),
            "github_org": cfg.get("github_org", slug),
            "telegram_chat_id": cfg.get("telegram_chat_id"),
            "telegram_group_title": cfg.get("telegram_group_title"),
            "telegram_group_username": cfg.get("telegram_group_username"),
            "neo4j_host": cfg.get("neo4j_host"),
            "neo4j_user": cfg.get("neo4j_user"),
            "neo4j_password": cfg.get("neo4j_password"),
        })
    return {"orgs": result}


# =============================================================================
# TRANSCRIPT COLLECTION (CASS pipeline)
# =============================================================================


MAX_TRANSCRIPT_SIZE = 50 * 1024 * 1024  # 50MB compressed


@app.post("/api/transcript/upload")
async def transcript_upload(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    author: str = Form(""),
    branch: str = Form(""),
    started_at: str = Form(""),
    ended_at: str = Form(""),
    message_count: int = Form(0),
    size_bytes: int = Form(0),
    org: dict = Depends(validate_api_key),
):
    """Upload a session transcript for the CASS distillation pipeline."""
    if not org.get("transcript_sharing"):
        raise HTTPException(status_code=403, detail="Transcript sharing not enabled for this org")

    from .services.transcripts import check_duplicate, store_transcript, index_transcript

    if await check_duplicate(org, session_id):
        return {"status": "duplicate", "session_id": session_id}

    file_data = await file.read()
    if len(file_data) > MAX_TRANSCRIPT_SIZE:
        raise HTTPException(status_code=413, detail="Transcript exceeds 50MB compressed limit")

    metadata = {
        "session_id": session_id,
        "author": author,
        "branch": branch,
        "started_at": started_at,
        "ended_at": ended_at,
        "message_count": message_count,
        "size_bytes": size_bytes,
    }

    try:
        storage_path = await store_transcript(org, session_id, file_data, metadata)
        metadata["storage_path"] = storage_path
    except Exception as e:
        logger.error(f"Transcript storage failed for {session_id}: {e}")
        raise HTTPException(status_code=503, detail="Failed to store transcript")

    try:
        await index_transcript(org, metadata)
    except Exception as e:
        logger.warning(f"Transcript indexing failed for {session_id} (stored OK): {e}")

    return {"status": "uploaded", "session_id": session_id, "storage_path": storage_path}


# =============================================================================
# TELEMETRY INGESTION
# =============================================================================


MAX_TELEMETRY_BODY = 2 * 1024 * 1024  # 2MB max payload


@app.post("/api/telemetry/ingest")
async def telemetry_ingest(request: Request, org: dict = Depends(validate_api_key)):
    """Ingest telemetry events from client JSONL buffer.

    Body: NDJSON (one JSON object per line).
    Auth: Bearer {EGREGORE_API_KEY} (same as graph/notify).
    Server resolves org_slug from API key — never trusts client-sent org.
    """
    if not USE_SUPABASE:
        return {"ingested": 0, "reason": "telemetry requires supabase"}

    body = await request.body()
    if len(body) > MAX_TELEMETRY_BODY:
        raise HTTPException(status_code=413, detail="Telemetry payload exceeds 2MB limit")

    # Parse NDJSON
    events = []
    for line in body.decode("utf-8", errors="replace").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # Skip malformed lines

    if not events:
        return {"ingested": 0}

    org_slug = org.get("slug", "unknown")

    try:
        from .services.supabase import ingest_telemetry_events
        count = ingest_telemetry_events(org_slug, events)
        return {"ingested": count}
    except Exception as e:
        logger.error(f"Telemetry ingestion failed for {org_slug}: {e}")
        raise HTTPException(status_code=503, detail="Failed to ingest telemetry events")


# =============================================================================
# HEALTH CHECK-IN
# =============================================================================


@app.post("/api/health/checkin")
async def health_checkin(body: HealthCheckin, github_username: str = Depends(validate_github_token)):
    """Record a health check-in from a client session at startup.

    Auth: GitHub token (NOT API key — broken keys are the #1 problem).
    Called by bin/startup-check.sh in the background at session start.
    """
    if not USE_SUPABASE:
        return {"status": "skipped", "reason": "supabase disabled"}

    from .services.supabase import insert_health_checkin

    try:
        row = insert_health_checkin(
            github_username=github_username,
            org_slug=body.org_slug,
            key_valid=body.key_valid,
            key_slug=body.key_slug,
            config_slug=body.config_slug,
            framework_version=body.framework_version,
            memory_linked=body.memory_linked,
            git_synced=body.git_synced,
            branch=body.branch,
            errors=body.errors,
            platform=body.platform,
            shell=body.shell,
        )
        return {"status": "ok", "id": row.get("id")}
    except Exception as e:
        logger.warning(f"Health check-in failed for {github_username}: {e}")
        return {"status": "error", "detail": str(e)}


# =============================================================================
# ADMIN DASHBOARD
# =============================================================================


@app.get("/api/admin/dashboard")
async def admin_dashboard(admin_user: str = Depends(validate_admin_github_token)):
    """Admin overview: all orgs with health checks, member counts, telemetry stats."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Admin dashboard requires Supabase")

    from .services import supabase as sb

    try:
        orgs = sb.list_orgs()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_orgs failed: {e}")
    try:
        all_memberships = sb.get_all_memberships()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_all_memberships failed: {e}")
    try:
        all_keys = sb.list_api_keys()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_api_keys failed: {e}")
    try:
        recent_telemetry = sb.get_telemetry_events(limit=500)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_telemetry_events failed: {e}")

    # Index memberships by org
    memberships_by_org = {}
    for m in all_memberships:
        slug = m.get("org_slug", "")
        memberships_by_org.setdefault(slug, []).append(m)

    # Index keys by org
    keys_by_org = {}
    for k in all_keys:
        slug = k.get("org_slug", "")
        keys_by_org.setdefault(slug, []).append(k)

    # Index telemetry by org for last_activity
    last_activity_by_org = {}
    telemetry_count_by_org = {}
    for evt in recent_telemetry:
        slug = evt.get("org_slug", "")
        ts = evt.get("ts", "")
        if slug not in last_activity_by_org or ts > last_activity_by_org[slug]:
            last_activity_by_org[slug] = ts
        telemetry_count_by_org[slug] = telemetry_count_by_org.get(slug, 0) + 1

    # Get session counts + Org node status from Neo4j (best-effort)
    session_counts = {}
    neo4j_org_nodes = {}  # slug → {has_api_key, api_key_slug_match}
    seed_org = _get_seed_org()
    if seed_org:
        try:
            result = await execute_system_query(seed_org, """
                MATCH (s:Session)
                WITH s.org AS org, count(s) AS cnt
                RETURN org, cnt
            """)
            for row in result.get("values", []):
                if row and len(row) >= 2 and row[0]:
                    session_counts[row[0]] = row[1]
        except Exception as e:
            logger.warning(f"Admin dashboard: Neo4j session count failed: {e}")

        # Check Org nodes for API key presence and slug match
        try:
            result = await execute_system_query(seed_org, """
                MATCH (o:Org)
                RETURN o.id AS slug,
                       o.api_key IS NOT NULL AS has_key,
                       CASE WHEN o.api_key IS NOT NULL
                            THEN split(o.api_key, '_')[1]
                            ELSE null END AS key_slug
            """)
            for row in result.get("values", []):
                if row and row[0]:
                    neo4j_org_nodes[row[0]] = {
                        "has_api_key": bool(row[1]),
                        "key_slug_match": row[2] == row[0] if row[2] else None,
                    }
        except Exception as e:
            logger.warning(f"Admin dashboard: Neo4j Org node check failed: {e}")

    all_alerts = []
    org_results = []

    for org_row in orgs:
        slug = org_row["slug"]
        org_keys = keys_by_org.get(slug, [])
        org_members = memberships_by_org.get(slug, [])
        active_keys = [k for k in org_keys if k.get("is_active")]

        health = []

        # Health check: key slug mismatch
        for k in active_keys:
            prefix = k.get("key_prefix", "")
            # ek_{slug}_{hash8} — extract slug part
            parts = prefix.split("_")
            if len(parts) >= 3:
                key_slug = parts[1]
                if key_slug != slug:
                    issue = {
                        "type": "key_slug_mismatch",
                        "severity": "critical",
                        "detail": f"Key prefix ek_{key_slug}_... doesn't match org slug {slug}",
                    }
                    health.append(issue)
                    all_alerts.append({**issue, "org_slug": slug})

        # Health check: no active API key
        if not active_keys:
            issue = {"type": "no_active_key", "severity": "warning", "detail": "No active API key"}
            health.append(issue)
            all_alerts.append({**issue, "org_slug": slug})

        # Health check: no Telegram
        if not org_row.get("telegram_chat_id"):
            health.append({"type": "no_telegram", "severity": "info", "detail": "Telegram not connected"})

        # Health check: no active members
        active_members = [m for m in org_members if m.get("status") == "active"]
        if not active_members:
            issue = {"type": "no_active_members", "severity": "warning", "detail": "No active members"}
            health.append(issue)
            all_alerts.append({**issue, "org_slug": slug})

        # Health check: Neo4j Org node missing or has no API key
        neo4j_node = neo4j_org_nodes.get(slug)
        if not neo4j_node:
            issue = {"type": "no_neo4j_org_node", "severity": "warning", "detail": "No Org node in Neo4j graph"}
            health.append(issue)
            all_alerts.append({**issue, "org_slug": slug})
        elif not neo4j_node.get("has_api_key"):
            issue = {"type": "neo4j_no_api_key", "severity": "warning", "detail": "Org node in Neo4j has no api_key"}
            health.append(issue)
            all_alerts.append({**issue, "org_slug": slug})
        elif neo4j_node.get("key_slug_match") is False:
            issue = {"type": "neo4j_key_slug_mismatch", "severity": "critical",
                     "detail": "Org node api_key slug doesn't match org slug"}
            health.append(issue)
            all_alerts.append({**issue, "org_slug": slug})

        # Neo4j host info
        neo4j_host = org_row.get("neo4j_host", "")
        neo4j_host_short = neo4j_host.split(".")[0] if neo4j_host else ""

        org_results.append({
            "slug": slug,
            "name": org_row.get("name", slug),
            "github_org": org_row.get("github_org", ""),
            "created_at": org_row.get("created_at"),
            "created_by": org_row.get("created_by"),
            "member_count": len(active_members),
            "members": [
                m["users"].get("github_username", "")
                for m in active_members if m.get("users")
            ],
            "last_activity": last_activity_by_org.get(slug),
            "telegram_connected": bool(org_row.get("telegram_chat_id")),
            "telegram_group_title": org_row.get("telegram_group_title"),
            "has_active_key": bool(active_keys),
            "key_prefix": active_keys[0]["key_prefix"] if active_keys else None,
            "session_count": session_counts.get(slug, 0),
            "neo4j_host": neo4j_host_short,
            "neo4j_org_node": bool(neo4j_node),
            "neo4j_has_key": neo4j_node.get("has_api_key", False) if neo4j_node else False,
            "health": health,
        })

    unique_users = set()
    for m in all_memberships:
        if m.get("status") == "active" and m.get("users"):
            unique_users.add(m["users"].get("github_username", ""))

    return {
        "orgs": org_results,
        "total_orgs": len(orgs),
        "total_users": len(unique_users),
        "alerts": [a for a in all_alerts if a.get("severity") in ("critical", "warning")],
    }


@app.get("/api/admin/org/{slug}")
async def admin_org_detail(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Detailed admin view for a single org."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Admin dashboard requires Supabase")

    from .services import supabase as sb

    org_row = sb.get_org_by_slug(slug)
    if not org_row:
        raise HTTPException(status_code=404, detail=f"Org not found: {slug}")

    try:
        members = sb.get_memberships(slug)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_memberships failed: {e}")
    try:
        telemetry = sb.get_telemetry_events(org_slug=slug, limit=50)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"get_telemetry_events failed: {e}")

    # API keys (prefix only)
    try:
        all_keys = sb.list_api_keys()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_api_keys failed: {e}")
    org_keys = [
        {"key_prefix": k["key_prefix"], "is_active": k["is_active"], "created_at": k.get("created_at")}
        for k in all_keys
        if k.get("org_slug") == slug
    ]

    # Neo4j node counts + isolation checks (best-effort)
    neo4j_stats = {}
    isolation = {"status": "unknown", "checks": []}
    neo4j_org_node_info = {}
    seed_org = _get_seed_org()
    if seed_org:
        try:
            for label in ["Person", "Session", "Quest", "Artifact"]:
                result = await execute_system_query(seed_org, f"""
                    MATCH (n:{label}) WHERE n.org = $slug
                    RETURN count(n) AS cnt
                """, {"slug": slug})
                vals = result.get("values", [])
                neo4j_stats[label] = vals[0][0] if vals and vals[0] else 0
        except Exception as e:
            logger.warning(f"Admin org detail: Neo4j stats failed for {slug}: {e}")

        # Check Org node in Neo4j
        try:
            result = await execute_system_query(seed_org, """
                MATCH (o:Org {id: $slug})
                RETURN o.api_key IS NOT NULL AS has_key,
                       CASE WHEN o.api_key IS NOT NULL
                            THEN split(o.api_key, '_')[1]
                            ELSE null END AS key_slug,
                       o.name AS name
            """, {"slug": slug})
            vals = result.get("values", [])
            if vals and vals[0]:
                neo4j_org_node_info = {
                    "exists": True,
                    "has_api_key": bool(vals[0][0]),
                    "key_slug_match": vals[0][1] == slug if vals[0][1] else None,
                }
            else:
                neo4j_org_node_info = {"exists": False, "has_api_key": False, "key_slug_match": None}
        except Exception as e:
            logger.warning(f"Admin org detail: Org node check failed for {slug}: {e}")

        # Isolation check: verify no cross-org data leakage
        try:
            # Check if any nodes with wrong org scope exist on this org's database
            result = await execute_system_query(seed_org, """
                MATCH (n)
                WHERE n.org IS NOT NULL AND n.org <> $slug
                  AND NOT n:Org AND NOT n:TelegramUser
                WITH n.org AS other_org, labels(n)[0] AS label, count(n) AS cnt
                RETURN other_org, label, cnt
                ORDER BY cnt DESC LIMIT 5
            """, {"slug": slug})
            vals = result.get("values", [])
            if vals and vals[0] and vals[0][0]:
                # Other org data found on same database — expected for shared DB
                other_orgs = [{"org": row[0], "label": row[1], "count": row[2]} for row in vals if row[0]]
                isolation["checks"].append({
                    "check": "shared_database",
                    "status": "info",
                    "detail": f"Shared DB with {len(set(r['org'] for r in other_orgs))} other org(s) — isolation via org scope",
                    "other_orgs": [r["org"] for r in other_orgs[:5]],
                })
            else:
                isolation["checks"].append({
                    "check": "dedicated_database",
                    "status": "ok",
                    "detail": "Dedicated database — no other org data present",
                })
        except Exception as e:
            logger.warning(f"Admin org detail: isolation check failed for {slug}: {e}")

        # Isolation check: verify org scope is present on this org's data
        try:
            result = await execute_system_query(seed_org, """
                MATCH (s:Session)
                WHERE s.org = $slug
                RETURN count(s) AS scoped
                UNION ALL
                MATCH (s:Session)
                WHERE s.org IS NULL
                RETURN count(s) AS scoped
            """, {"slug": slug})
            vals = result.get("values", [])
            scoped = vals[0][0] if vals and vals[0] else 0
            unscoped = vals[1][0] if vals and len(vals) > 1 and vals[1] else 0
            if unscoped > 0:
                isolation["checks"].append({
                    "check": "unscoped_sessions",
                    "status": "warning",
                    "detail": f"{unscoped} sessions missing org scope (pre-migration data)",
                })
            else:
                isolation["checks"].append({
                    "check": "all_sessions_scoped",
                    "status": "ok",
                    "detail": f"All {scoped} sessions properly scoped to '{slug}'",
                })
        except Exception as e:
            logger.warning(f"Admin org detail: scope check failed for {slug}: {e}")

        # Derive overall isolation status
        statuses = [c["status"] for c in isolation["checks"]]
        if "warning" in statuses or "critical" in statuses:
            isolation["status"] = "warning"
        elif statuses:
            isolation["status"] = "ok"

    # Health diagnostics (same logic as dashboard + isolation)
    health = []
    active_keys = [k for k in org_keys if k.get("is_active")]
    for k in active_keys:
        parts = k["key_prefix"].split("_")
        if len(parts) >= 3 and parts[1] != slug:
            health.append({
                "type": "key_slug_mismatch", "severity": "critical",
                "detail": f"Key prefix {k['key_prefix']} doesn't match org slug {slug}",
            })
    if not active_keys:
        health.append({"type": "no_active_key", "severity": "warning", "detail": "No active API key"})
    if not org_row.get("telegram_chat_id"):
        health.append({"type": "no_telegram", "severity": "info", "detail": "Telegram not connected"})
    active_members = [m for m in members if m.get("status") == "active"]
    if not active_members:
        health.append({"type": "no_active_members", "severity": "warning", "detail": "No active members"})

    # Health: Neo4j Org node
    if neo4j_org_node_info.get("exists") is False:
        health.append({"type": "no_neo4j_org_node", "severity": "warning", "detail": "No Org node in Neo4j graph"})
    elif not neo4j_org_node_info.get("has_api_key"):
        health.append({"type": "neo4j_no_api_key", "severity": "warning", "detail": "Org node in Neo4j has no api_key"})
    elif neo4j_org_node_info.get("key_slug_match") is False:
        health.append({"type": "neo4j_key_slug_mismatch", "severity": "critical",
                       "detail": "Org node api_key slug doesn't match org slug"})

    # Neo4j Person nodes (includes Telegram-only users not in Supabase)
    graph_persons = []
    if seed_org:
        try:
            result = await execute_system_query(seed_org, """
                MATCH (p:Person {org: $slug})
                RETURN p.name AS name, p.github AS github
                ORDER BY p.name
            """, {"slug": slug})
            vals = result.get("values", [])
            fields = result.get("fields", [])
            graph_persons = [dict(zip(fields, row)) for row in vals] if vals else []
        except Exception as e:
            logger.warning(f"Admin org detail: graph persons failed for {slug}: {e}")

    # Org config (redact neo4j password)
    config = {
        "slug": org_row["slug"],
        "name": org_row.get("name", ""),
        "github_org": org_row.get("github_org", ""),
        "created_at": org_row.get("created_at"),
        "created_by": org_row.get("created_by"),
        "neo4j_host": org_row.get("neo4j_host", ""),
        "telegram_chat_id": org_row.get("telegram_chat_id"),
        "telegram_group_title": org_row.get("telegram_group_title"),
        "transcript_sharing": org_row.get("transcript_sharing", False),
        "repo_name": org_row.get("repo_name"),
        "managed_repos": org_row.get("managed_repos"),
        "hosting_enabled": org_row.get("hosting_enabled"),
        "hosting_ip": org_row.get("hosting_ip"),
        "hosting_coder_url": org_row.get("hosting_coder_url"),
    }

    members_list = [
        {
            "github_username": m["users"]["github_username"] if m.get("users") else None,
            "github_name": m["users"]["github_name"] if m.get("users") else None,
            "display_name": m.get("display_name"),
            "telegram_username": m["users"]["telegram_username"] if m.get("users") else None,
            "role": m.get("role"),
            "status": m.get("status"),
            "joined_at": m.get("joined_at"),
        }
        for m in members
    ]

    return {
        "config": config,
        "members": members_list,
        "graph_persons": graph_persons,
        "api_keys": org_keys,
        "telemetry": telemetry,
        "neo4j_stats": neo4j_stats,
        "neo4j_org_node": neo4j_org_node_info,
        "isolation": isolation,
        "health": health,
    }


@app.patch("/api/admin/org/{slug}")
async def admin_patch_org(
    slug: str,
    body: dict,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Patch org fields in Supabase. Allowed fields: created_at, name, transcript_sharing."""
    from .services import supabase as sb

    org_row = sb.get_org_by_slug(slug)
    if not org_row:
        raise HTTPException(status_code=404, detail=f"Org not found: {slug}")

    allowed = {"created_at", "name", "transcript_sharing",
                "hosting_enabled", "hosting_ip", "hosting_coder_url",
                "hosting_coder_token", "hosting_server_id",
                "telegram_chat_id", "telegram_group_title", "telegram_group_username",
                "repo_name", "managed_repos"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail=f"No allowed fields. Allowed: {allowed}")

    try:
        sb.get_client().table("orgs").update(updates).eq("slug", slug).execute()
        # Sync telegram fields to in-memory config (avoids restart)
        for field in ("telegram_chat_id", "telegram_group_title", "telegram_group_username"):
            if field in updates and slug in ORG_CONFIGS:
                ORG_CONFIGS[slug][field] = updates[field]
        return {"patched": list(updates.keys()), "slug": slug}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Patch failed: {e}")


@app.patch("/api/admin/org/{slug}/members/{username}")
async def admin_patch_member(
    slug: str,
    username: str,
    body: dict,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Patch membership fields. Platform admin only."""
    from .services import supabase as sb

    allowed = {"role", "coder_username", "status"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail=f"No allowed fields. Allowed: {allowed}")

    user = sb.get_user_by_github(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User not found: {username}")

    try:
        sb.get_client().table("memberships").update(updates).eq(
            "org_slug", slug
        ).eq("user_id", user["id"]).execute()
        return {"patched": list(updates.keys()), "slug": slug, "username": username}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Patch failed: {e}")


@app.post("/api/admin/org/{slug}/fix-key")
async def admin_fix_key(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Backfill api_key on Neo4j Org node from in-memory config.

    For orgs created via /register (which didn't write the key to Neo4j),
    this copies the plaintext key from ORG_CONFIGS to the Org node.
    """
    org_config = ORG_CONFIGS.get(slug)
    if not org_config:
        raise HTTPException(status_code=404, detail=f"Org not found in config: {slug}")

    api_key = org_config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"No plaintext key in memory for {slug}. API may have restarted since creation. "
                   "Use /api/admin/org/{slug}/rotate-key to generate a new one.",
        )

    try:
        result = await execute_system_query(org_config, """
            MATCH (o:Org {id: $slug})
            SET o.api_key = $api_key
            RETURN o.id AS slug
        """, {"slug": slug, "api_key": api_key})
        vals = result.get("values", [])
        if not vals or not vals[0]:
            return {"status": "warning", "detail": f"Org node not found for {slug}. Key not written."}
        return {"status": "ok", "detail": f"api_key written to Neo4j Org node for {slug}"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Neo4j write failed: {e}")


@app.post("/api/admin/org/{slug}/rotate-key")
async def admin_rotate_key(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Generate a new API key for an org. Revokes old key, writes to Supabase + Neo4j + memory.

    Use when: plaintext key is lost (API restarted, Neo4j Org node missing key).
    """
    org_config = ORG_CONFIGS.get(slug)
    if not org_config:
        raise HTTPException(status_code=404, detail=f"Org not found in config: {slug}")

    from .services import supabase as sb

    # Generate new key
    new_key = generate_api_key(slug)

    # Revoke old keys in Supabase
    sb.revoke_api_key(slug)

    # Store new key hash in Supabase
    sb.create_api_key(slug, new_key)

    # Write plaintext to Neo4j Org node
    try:
        await execute_system_query(org_config, """
            MATCH (o:Org {id: $slug})
            SET o.api_key = $api_key
            RETURN o.id
        """, {"slug": slug, "api_key": new_key})
    except Exception as e:
        logger.warning(f"rotate-key: Neo4j write failed for {slug}: {e}")

    # Update in-memory config
    org_config["api_key"] = new_key
    ORG_CONFIGS[slug]["api_key"] = new_key

    return {
        "status": "ok",
        "slug": slug,
        "key_prefix": new_key[:20] + "...",
        "detail": "New key generated. Old key revoked. Written to Supabase + Neo4j + memory.",
    }


@app.post("/api/admin/backfill-keys")
async def admin_backfill_keys(admin_user: str = Depends(validate_admin_github_token)):
    """Backfill key_plaintext in Supabase from Neo4j Org nodes.

    For existing orgs where Supabase only has the hash. Reads plaintext from
    Neo4j and writes it to the key_plaintext column in Supabase.
    """
    from .services import supabase as sb

    seed_org = _get_seed_org()
    if not seed_org:
        raise HTTPException(status_code=503, detail="No Neo4j access available")

    # Get all Org nodes with api_keys from Neo4j
    result = await execute_system_query(seed_org, """
        MATCH (o:Org)
        WHERE o.api_key IS NOT NULL
        RETURN o.id AS slug, o.api_key AS api_key
    """)

    backfilled = []
    skipped = []
    failed = []

    for row in result.get("values", []):
        if not row or not row[0] or not row[1]:
            continue
        slug, neo4j_key = row[0], row[1]

        # Check if Supabase already has plaintext for this org
        existing = sb.get_active_api_key_plaintext(slug)
        if existing:
            skipped.append(slug)
            continue

        # Write plaintext to Supabase (update existing active key row)
        try:
            prefix = neo4j_key[:20] if len(neo4j_key) > 20 else neo4j_key
            # Find the active key row and update it
            get_client = sb.get_client
            update_result = (
                get_client()
                .table("api_keys")
                .update({"key_plaintext": neo4j_key})
                .eq("org_slug", slug)
                .eq("is_active", True)
                .execute()
            )
            if update_result.data:
                backfilled.append(slug)
                # Also update in-memory
                if slug in ORG_CONFIGS:
                    ORG_CONFIGS[slug]["api_key"] = neo4j_key
            else:
                failed.append({"slug": slug, "reason": "no active key row in Supabase"})
        except Exception as e:
            failed.append({"slug": slug, "reason": str(e)})

    return {
        "status": "ok",
        "backfilled": backfilled,
        "skipped": skipped,
        "failed": failed,
    }


@app.post("/api/admin/graph-query")
async def admin_graph_query(
    body: dict,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Run a read-only Cypher query against the shared customer Neo4j database.

    Body: {"statement": "MATCH (n) RETURN n LIMIT 5", "params": {}}
    Only for admin inspection — no writes allowed.
    """
    statement = body.get("statement", "")
    if not statement:
        raise HTTPException(status_code=400, detail="Missing 'statement' field")

    # Block write operations
    upper = statement.upper().strip()
    for keyword in ("CREATE", "MERGE", "DELETE", "DETACH", "SET ", "REMOVE "):
        if keyword in upper:
            raise HTTPException(status_code=400, detail=f"Write operations not allowed: {keyword.strip()}")

    seed_org = _get_seed_org()
    if not seed_org:
        raise HTTPException(status_code=503, detail="No Neo4j access available")

    try:
        result = await execute_system_query(seed_org, statement, body.get("params", {}))
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Query failed: {e}")


@app.post("/api/admin/graph-write")
async def admin_graph_write(
    body: dict,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Run a write Cypher query against the shared customer Neo4j database.

    Body: {"statement": "MATCH (n) ...", "params": {}}
    Admin-only. For cleanup and migration operations.
    """
    statement = body.get("statement", "")
    if not statement:
        raise HTTPException(status_code=400, detail="Missing 'statement' field")

    seed_org = _get_seed_org()
    if not seed_org:
        raise HTTPException(status_code=503, detail="No Neo4j access available")

    try:
        result = await execute_system_query(seed_org, statement, body.get("params", {}))
        return result
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Query failed: {e}")


@app.post("/api/admin/org/{old_slug}/rename")
async def admin_rename_org(
    old_slug: str,
    body: dict,
    admin_user: str = Depends(validate_admin_github_token),
):
    """Rename an org slug across all systems.

    Body: {"new_slug": "egregore-0"}

    Updates: Supabase (orgs, api_keys, memberships, telemetry_events, telegram_events),
    Neo4j (Org node id, all org-scoped nodes), API key (regenerated with new prefix),
    and in-memory ORG_CONFIGS.
    """
    new_slug = body.get("new_slug", "").strip()
    if not new_slug:
        raise HTTPException(status_code=400, detail="Missing 'new_slug' field")
    if new_slug == old_slug:
        raise HTTPException(status_code=400, detail="New slug is same as old slug")

    from .services import supabase as sb

    # Verify old org exists
    old_org = sb.get_org_by_slug(old_slug)
    if not old_org:
        raise HTTPException(status_code=404, detail=f"Org not found: {old_slug}")

    # Check new slug doesn't already exist (unless it's an empty ghost we'll merge into)
    existing_new = sb.get_org_by_slug(new_slug)

    steps = []
    get_client = sb.get_client

    # Step 1: Clean up ghost org at new_slug if it exists
    if existing_new:
        try:
            get_client().table("memberships").delete().eq("org_slug", new_slug).execute()
            get_client().table("api_keys").delete().eq("org_slug", new_slug).execute()
            get_client().table("telemetry_events").delete().eq("org_slug", new_slug).execute()
            get_client().table("telegram_events").delete().eq("org_slug", new_slug).execute()
            get_client().table("orgs").delete().eq("slug", new_slug).execute()
            steps.append(f"Deleted ghost org '{new_slug}' from Supabase")
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Failed to clean up ghost org: {e}")

    # Step 2: Null out unique fields on old row, then create new row with those values
    try:
        # Clear unique-constrained fields on old row first
        get_client().table("orgs").update({
            "telegram_chat_id": None,
            "telegram_group_title": None,
            "telegram_group_username": None,
        }).eq("slug", old_slug).execute()

        new_org_data = {
            "slug": new_slug,
            "name": old_org.get("name", ""),
            "github_org": old_org.get("github_org", ""),
            "neo4j_host": old_org.get("neo4j_host", ""),
            "neo4j_user": old_org.get("neo4j_user", "neo4j"),
            "neo4j_password": old_org.get("neo4j_password", ""),
            "created_by": old_org.get("created_by"),
            "created_at": old_org.get("created_at"),
            "telegram_chat_id": old_org.get("telegram_chat_id"),
            "telegram_group_title": old_org.get("telegram_group_title"),
            "telegram_group_username": old_org.get("telegram_group_username"),
            "transcript_sharing": old_org.get("transcript_sharing", False),
        }
        get_client().table("orgs").insert(new_org_data).execute()
        steps.append(f"Created new org row: {new_slug}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to create new org row: {e}")

    # Step 3: Migrate all FK references from old_slug → new_slug
    for table in ("api_keys", "memberships", "telemetry_events", "telegram_events"):
        try:
            get_client().table(table).update({"org_slug": new_slug}).eq("org_slug", old_slug).execute()
            steps.append(f"Migrated {table}: {old_slug} → {new_slug}")
        except Exception as e:
            steps.append(f"Warning: {table} migration failed: {e}")

    # Step 4: Delete old org row (now safe — no FK references left)
    try:
        get_client().table("orgs").delete().eq("slug", old_slug).execute()
        steps.append(f"Deleted old org row: {old_slug}")
    except Exception as e:
        steps.append(f"Warning: old org row deletion failed: {e}")

    # Step 3: Regenerate API key with new slug prefix
    try:
        sb.revoke_api_key(old_slug)  # revoke by old slug (already renamed, try both)
        sb.revoke_api_key(new_slug)
    except Exception:
        pass
    new_key = generate_api_key(new_slug)
    try:
        sb.create_api_key(new_slug, new_key)
        steps.append(f"Generated new API key: {new_key[:20]}...")
    except Exception as e:
        steps.append(f"Warning: API key creation failed: {e}")

    # Step 4: Update Neo4j
    seed_org = _get_seed_org()
    if seed_org:
        try:
            # Rename Org node
            await execute_system_query(seed_org, """
                MATCH (o:Org {id: $old_slug})
                SET o.id = $new_slug, o.org = $new_slug, o.api_key = $new_key
                RETURN o.id
            """, {"old_slug": old_slug, "new_slug": new_slug, "new_key": new_key})
            steps.append(f"Renamed Neo4j Org node: {old_slug} → {new_slug}")
        except Exception as e:
            steps.append(f"Warning: Neo4j Org rename failed: {e}")

        try:
            # Delete ghost Org node if it existed
            await execute_system_query(seed_org, """
                MATCH (o:Org {id: $new_slug})
                WITH o, count {{(o2:Org {{id: $new_slug}})}} AS cnt
                WHERE cnt > 1
                DELETE o
            """, {"new_slug": new_slug})
        except Exception:
            pass  # Best-effort ghost cleanup

        # Update org scope on ALL node types
        for label in ("Person", "Session", "Artifact", "Quest", "Project", "Spirit", "CheckIn", "Todo", "QuestionSet"):
            try:
                result = await execute_system_query(seed_org, f"""
                    MATCH (n:{label}) WHERE n.org = $old_slug
                    SET n.org = $new_slug
                    RETURN count(n) AS updated
                """, {"old_slug": old_slug, "new_slug": new_slug})
                vals = result.get("values", [])
                count = vals[0][0] if vals and vals[0] else 0
                if count > 0:
                    steps.append(f"Updated {count} {label} nodes: org → {new_slug}")
            except Exception as e:
                steps.append(f"Warning: {label} scope update failed: {e}")

    # Step 5: Update in-memory ORG_CONFIGS
    old_config = ORG_CONFIGS.pop(old_slug, {})
    old_config["api_key"] = new_key
    old_config["slug"] = new_slug
    ORG_CONFIGS[new_slug] = old_config
    steps.append(f"Updated in-memory config: {old_slug} → {new_slug}")

    return {
        "status": "ok",
        "old_slug": old_slug,
        "new_slug": new_slug,
        "new_key_prefix": new_key[:20] + "...",
        "steps": steps,
    }


@app.get("/api/admin/telemetry")
async def admin_telemetry(
    org_slug: str = Query(None),
    event_type: str = Query(None),
    user_handle: str = Query(None),
    since: str = Query(None),
    limit: int = Query(100, le=500),
    admin_user: str = Depends(validate_admin_github_token),
):
    """Filterable telemetry feed for admins."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Telemetry requires Supabase")

    from .services import supabase as sb

    events = sb.get_telemetry_events(
        org_slug=org_slug,
        event_type=event_type,
        user_handle=user_handle,
        since=since,
        limit=limit,
    )

    # Compute aggregates
    by_type = {}
    by_org = {}
    for evt in events:
        t = evt.get("type", "unknown")
        o = evt.get("org_slug", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        by_org[o] = by_org.get(o, 0) + 1

    return {
        "events": events,
        "count": len(events),
        "aggregates": {
            "by_type": by_type,
            "by_org": by_org,
        },
    }


# =============================================================================
# USER DASHBOARD (any authenticated GitHub user)
# =============================================================================


def _build_workspace_url(org_data: dict, membership: dict) -> str:
    """Build direct workspace terminal URL for this user on the hosted Coder."""
    coder_url = (org_data.get("hosting_coder_url") or "").rstrip("/")
    if not org_data.get("hosting_enabled") or not coder_url:
        return ""
    coder_user = membership.get("coder_username") or ""
    if not coder_user:
        return coder_url
    return f"{coder_url}/@{coder_user}/egregore.main/terminal"


@app.get("/api/me/egregores")
async def me_egregores(github_username: str = Depends(validate_github_token)):
    """User dashboard: return ONLY the authenticated user's orgs with full detail.

    Includes: slug, name, role, API key (full + masked), member list,
    latest health check-in, and health diagnostics (key mismatch → fix command).

    Scoping enforced at API level: queries memberships by authenticated user's user_id.
    No parameter the user can manipulate to see other orgs.
    """
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Dashboard requires Supabase")

    from .services import supabase as sb

    # Get user's memberships (scoped by user_id)
    memberships = sb.get_user_orgs(github_username)
    if not memberships:
        return {"github_username": github_username, "egregores": []}

    # Get user's health check-ins
    health_checkins = sb.get_user_health_checkins(github_username, limit=20)

    # Index check-ins by org (latest per org)
    latest_checkin_by_org = {}
    for c in health_checkins:
        org = c.get("org_slug", "")
        if org not in latest_checkin_by_org:
            latest_checkin_by_org[org] = c

    egregores = []
    for m in memberships:
        org_data = m.get("orgs")
        if not org_data:
            continue

        slug = org_data["slug"]
        role = m.get("role", "member")

        # Get API key for this org (full plaintext for the user)
        api_key = sb.get_active_api_key_plaintext(slug) or ""
        masked_key = ""
        if api_key:
            # ek_slug_abc123... → ek_slug_abc1****
            parts = api_key.split("_")
            if len(parts) >= 3:
                secret = parts[2]
                masked_key = f"ek_{parts[1]}_{secret[:4]}{'*' * (len(secret) - 4)}"
            else:
                masked_key = api_key[:8] + "****"

        # Get members for this org
        org_members = sb.get_memberships(slug)
        members_list = [
            {
                "github_username": mem["users"]["github_username"] if mem.get("users") else None,
                "github_name": mem["users"]["github_name"] if mem.get("users") else None,
                "display_name": mem.get("display_name"),
                "role": mem.get("role", "member"),
                "status": mem.get("status", "active"),
            }
            for mem in org_members
            if mem.get("users")
        ]

        # Latest health check-in for this org
        # Try exact slug match first, then github_org variants (egregore.json
        # slug may differ from Supabase slug, e.g. "curvelabs" vs "egregore-0")
        github_org = org_data.get("github_org", "")
        checkin = (
            latest_checkin_by_org.get(slug)
            or latest_checkin_by_org.get(github_org.lower().replace("-", ""))
            or latest_checkin_by_org.get(github_org.lower())
        )

        # Health diagnostics
        diagnostics = []
        if checkin:
            if checkin.get("key_valid") is False:
                key_slug = checkin.get("key_slug", "")
                config_slug = checkin.get("config_slug", "")
                fix_cmd = f'sed -i.bak "s/^EGREGORE_API_KEY=.*/EGREGORE_API_KEY=<correct-key>/" .env'
                diagnostics.append({
                    "type": "key_mismatch",
                    "severity": "critical",
                    "detail": f"API key slug '{key_slug}' doesn't match config slug '{config_slug}'",
                    "fix_command": fix_cmd,
                    "correct_key": api_key,
                })
            if checkin.get("memory_linked") is False:
                diagnostics.append({
                    "type": "memory_not_linked",
                    "severity": "warning",
                    "detail": "Memory directory not symlinked",
                })
            if checkin.get("git_synced") is False:
                diagnostics.append({
                    "type": "git_behind",
                    "severity": "warning",
                    "detail": "Local branch is behind develop",
                })
            for err in (checkin.get("errors") or []):
                if "key_slug_mismatch" in str(err):
                    continue  # Already covered above
                diagnostics.append({
                    "type": "error",
                    "severity": "warning",
                    "detail": str(err),
                })

        egregores.append({
            "slug": slug,
            "name": org_data.get("name", slug),
            "github_org": org_data.get("github_org", ""),
            "role": role,
            "api_key": api_key,
            "api_key_masked": masked_key,
            "has_telegram": bool(org_data.get("telegram_chat_id")),
            "hosting_enabled": bool(org_data.get("hosting_enabled")),
            "hosting_coder_url": org_data.get("hosting_coder_url", ""),
            "hosting_workspace_url": _build_workspace_url(org_data, m),
            "members": members_list,
            "latest_checkin": checkin,
            "diagnostics": diagnostics,
        })

    return {"github_username": github_username, "egregores": egregores}


# =============================================================================
# ADMIN: HEALTH OVERVIEW
# =============================================================================


@app.get("/api/admin/health")
async def admin_health(
    org_slug: str = Query(None),
    admin_user: str = Depends(validate_admin_github_token),
):
    """Admin health overview: all users' latest check-ins with computed alerts.

    Returns: per-user latest check-in, plus aggregated alerts for:
    - Stale check-ins (>24h)
    - Broken keys (key_valid=false)
    - Version mismatches
    - Memory not linked
    """
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Health dashboard requires Supabase")

    from .services import supabase as sb

    checkins = sb.get_latest_health_checkins(org_slug=org_slug, limit=500)

    # Deduplicate: keep latest per (github_username, org_slug)
    checkin_map = {}  # (username, org) → checkin
    for c in checkins:
        key = (c.get("github_username", ""), c.get("org_slug", ""))
        if key not in checkin_map:
            checkin_map[key] = c

    # Get ALL memberships to show users who haven't checked in
    all_memberships = sb.get_all_memberships()
    if org_slug:
        all_memberships = [m for m in all_memberships if m.get("org_slug") == org_slug]

    # Build org info lookup (for Telegram group names)
    orgs_list = sb.list_orgs()
    org_info = {}
    for o in orgs_list:
        org_info[o["slug"]] = {
            "name": o.get("name", ""),
            "github_org": o.get("github_org", ""),
            "telegram_group_title": o.get("telegram_group_title") or "",
            "telegram_chat_id": o.get("telegram_chat_id") or "",
        }

    # Build membership lookup: (username, org) → membership data (for display_name)
    membership_lookup = {}
    for m in all_memberships:
        user_info = m.get("users") or {}
        username = user_info.get("github_username", "")
        m_org = m.get("org_slug", "")
        if username:
            membership_lookup[(username, m_org)] = {
                "display_name": m.get("display_name"),
                "github_name": user_info.get("github_name"),
            }

    # Build combined list: checked-in users + not-checked-in members
    seen_users = set()
    combined = []

    # First: users with check-ins (enriched with display_name from membership)
    for key, c in checkin_map.items():
        seen_users.add(key)
        enriched = {**c, "checked_in": True}
        member_info = membership_lookup.get(key)
        if member_info:
            enriched["display_name"] = member_info.get("display_name")
            enriched["github_name"] = member_info.get("github_name")
        combined.append(enriched)

    # Second: members without check-ins (also try github_org variant matching)
    for m in all_memberships:
        user_info = m.get("users") or {}
        username = user_info.get("github_username", "")
        m_org = m.get("org_slug", "")
        if not username:
            continue

        key = (username, m_org)
        if key in seen_users:
            continue

        # Check if they checked in under a different slug (egregore.json vs Supabase)
        has_any_checkin = any(
            k[0] == username for k in checkin_map
        )
        if has_any_checkin:
            # Find their check-in under the other slug
            for ck, cv in checkin_map.items():
                if ck[0] == username and (username, m_org) not in seen_users:
                    combined.append({**cv, "org_slug": m_org, "checked_in": True})
                    seen_users.add(key)
                    break
        if key not in seen_users:
            combined.append({
                "github_username": username,
                "org_slug": m_org,
                "display_name": m.get("display_name"),
                "github_name": user_info.get("github_name"),
                "checked_in": False,
                "checked_in_at": None,
                "key_valid": None,
                "memory_linked": None,
                "git_synced": None,
                "framework_version": None,
                "branch": None,
                "errors": [],
                "role": m.get("role", "member"),
            })
            seen_users.add(key)

    # Compute alerts (only for users who have checked in)
    alerts = []
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    for c in combined:
        if not c.get("checked_in"):
            continue
        user = c.get("github_username", "unknown")
        org = c.get("org_slug", "unknown")

        # Stale check-in
        checked_in = c.get("checked_in_at")
        if checked_in:
            try:
                ts = datetime.fromisoformat(checked_in.replace("Z", "+00:00"))
                if (now - ts) > timedelta(hours=24):
                    alerts.append({
                        "type": "stale_checkin",
                        "severity": "warning",
                        "user": user,
                        "org": org,
                        "detail": f"Last check-in {checked_in}",
                    })
            except (ValueError, TypeError):
                pass

        # Broken key
        if c.get("key_valid") is False:
            alerts.append({
                "type": "broken_key",
                "severity": "critical",
                "user": user,
                "org": org,
                "detail": f"Key slug '{c.get('key_slug')}' != config slug '{c.get('config_slug')}'",
            })

        # Memory not linked
        if c.get("memory_linked") is False:
            alerts.append({
                "type": "memory_not_linked",
                "severity": "warning",
                "user": user,
                "org": org,
                "detail": "Memory directory not symlinked",
            })

    # Version spread
    versions = {}
    for c in combined:
        v = c.get("framework_version")
        if v:
            versions[v] = versions.get(v, 0) + 1
    if len(versions) > 1:
        for v, count in versions.items():
            alerts.append({
                "type": "version_mismatch",
                "severity": "warning",
                "user": "",
                "org": "",
                "detail": f"Framework version '{v}' used by {count} user(s)",
            })

    # Enrich each row with org info (Telegram group, org name)
    for c in combined:
        info = org_info.get(c.get("org_slug", ""), {})
        c["org_name"] = info.get("name", "")
        c["telegram_group"] = info.get("telegram_group_title", "")

    # Sort: checked-in first, then not checked in
    combined.sort(key=lambda x: (not x.get("checked_in"), x.get("github_username", "")))

    return {
        "checkins": combined,
        "alerts": alerts,
        "total_users": len(seen_users),
        "versions": versions,
        "org_info": org_info,
    }


# =============================================================================
# ADMIN: DELETE ORG
# =============================================================================


@app.delete("/api/admin/org/{slug}")
async def admin_delete_org(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Cascading delete of an org and all its data.

    Deletes: memberships, api_keys, telemetry_events, telegram_events, orgs row (Supabase),
    Org node (Neo4j), in-memory ORG_CONFIGS entry.

    Admin-only. Used for test cleanup and decommissioning orgs.
    """
    from .services import supabase as sb

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Delete requires Supabase")

    org_row = sb.get_org_by_slug(slug)
    if not org_row:
        raise HTTPException(status_code=404, detail=f"Org not found: {slug}")

    steps = []
    get_client = sb.get_client

    # Delete FK-dependent tables first, then the org row
    for table, col in [
        ("memberships", "org_slug"),
        ("api_keys", "org_slug"),
        ("telemetry_events", "org_slug"),
        ("telegram_events", "org_slug"),
        ("orgs", "slug"),
    ]:
        try:
            get_client().table(table).delete().eq(col, slug).execute()
            steps.append(f"Deleted {table}")
        except Exception as e:
            steps.append(f"Warning: {table} delete failed: {e}")

    # Delete Neo4j Org node
    seed_org = _get_seed_org()
    if seed_org:
        try:
            await execute_system_query(seed_org, """
                MATCH (o:Org {id: $slug}) DELETE o
            """, {"slug": slug})
            steps.append("Deleted Neo4j Org node")
        except Exception as e:
            steps.append(f"Warning: Neo4j Org node delete failed: {e}")

    # Remove from in-memory config
    ORG_CONFIGS.pop(slug, None)
    steps.append("Removed from ORG_CONFIGS")

    logger.info(f"Admin {admin_user} deleted org: {slug}")

    return {"status": "ok", "slug": slug, "steps": steps}


# =============================================================================
# HOSTING (Coder VPS provisioning)
# =============================================================================


@app.post("/api/hosting/provision")
async def hosting_provision(
    body: HostingProvision,
    admin_user: str = Depends(validate_admin_github_token),
    authorization: str = Header(...),
):
    """Provision a Hetzner VPS with Coder for an org. Admin only."""
    from .services.hosting import provision_vps

    # Extract raw GitHub token for VPS git operations
    raw_token = authorization.replace("Bearer ", "").strip()
    github_token = raw_token if not raw_token.startswith("ek_") else ""

    # Get org's API key for the workspace
    org_config = ORG_CONFIGS.get(body.org_slug)
    egregore_api_key = ""
    if org_config:
        egregore_api_key = await _get_org_api_key(org_config, body.org_slug)

    api_url = ""
    if org_config:
        api_url = org_config.get("api_url", os.environ.get("EGREGORE_API_URL", ""))
    if not api_url:
        api_url = os.environ.get("EGREGORE_API_URL", "https://egregore-production-55f2.up.railway.app")

    result = await provision_vps(
        org_slug=body.org_slug,
        org_name=body.org_name,
        github_org=body.github_org,
        repo_name=body.repo_name,
        fork_url=body.fork_url or f"https://github.com/{body.github_org}/{body.repo_name}.git",
        memory_url=body.memory_url or f"https://github.com/{body.github_org}/{body.github_org}-memory.git",
        api_url=api_url,
        egregore_api_key=egregore_api_key,
        managed_repos=body.managed_repos,
        server_type=body.server_type,
        github_token=github_token,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Store VPS info in Supabase for the org (including coder_password for later token retrieval)
    if USE_SUPABASE and result.get("ip"):
        try:
            from .services import supabase as sb
            update_data = {
                "hosting_ip": result["ip"],
                "hosting_server_id": str(result.get("server_id", "")),
                "hosting_coder_url": result.get("coder_url", ""),
                "hosting_enabled": True,
            }
            # Store coder password so we can obtain a session token later
            if result.get("coder_password"):
                update_data["hosting_coder_password"] = result["coder_password"]
            sb.get_client().table("orgs").update(update_data).eq("slug", body.org_slug).execute()
        except Exception as e:
            logger.warning(f"Failed to store hosting info in Supabase: {e}")

    logger.info(f"Admin {admin_user} provisioned VPS for {body.org_slug}")
    return result


@app.post("/api/hosting/enable/{slug}")
async def hosting_enable(
    slug: str,
    github_username: str = Depends(validate_github_token),
    authorization: str = Header(...),
):
    """Enable hosted Coder for an existing org. Org admin only.

    Derives fork_url, memory_url from existing Supabase org data.
    Provisions VPS, stores hosting info.
    """
    from .services.hosting import provision_vps
    from .services import supabase as sb

    # Extract raw GitHub token for VPS git operations
    raw_token = authorization.replace("Bearer ", "").strip()
    github_token = raw_token if not raw_token.startswith("ek_") else ""

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    # Verify caller is admin of this org or platform admin
    is_platform_admin = github_username.lower() in {u.lower() for u in ADMIN_USERS}
    user = sb.get_user_by_github(github_username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not is_platform_admin:
        membership = sb.get_client().table("memberships").select("role").eq(
            "org_slug", slug
        ).eq("user_id", user["id"]).eq("status", "active").execute()
        if not membership.data or membership.data[0].get("role") != "admin":
            raise HTTPException(status_code=403, detail="Only org admins can enable hosting")

    # Get org info
    org_rows = sb.get_client().table("orgs").select("*").eq("slug", slug).execute()
    if not org_rows.data:
        raise HTTPException(status_code=404, detail="Org not found")
    org = org_rows.data[0]

    if org.get("hosting_enabled"):
        raise HTTPException(status_code=400, detail="Hosting already enabled")

    github_org = org.get("github_org", "")
    org_name = org.get("name", slug)
    repo_name = org.get("repo_name", "")

    # Get org's API key
    org_config = ORG_CONFIGS.get(slug)
    egregore_api_key = ""
    if org_config:
        egregore_api_key = await _get_org_api_key(org_config, slug)

    api_url = ""
    if org_config:
        api_url = org_config.get("api_url", os.environ.get("EGREGORE_API_URL", ""))
    if not api_url:
        api_url = os.environ.get("EGREGORE_API_URL", "https://egregore-production-55f2.up.railway.app")

    # Provision VPS
    result = await provision_vps(
        org_slug=slug,
        org_name=org_name,
        github_org=github_org,
        repo_name=repo_name,
        fork_url=f"https://github.com/{github_org}/{repo_name}.git",
        memory_url=f"https://github.com/{github_org}/{github_org}-memory.git",
        api_url=api_url,
        egregore_api_key=egregore_api_key,
        github_token=github_token,
    )

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Store VPS info in Supabase
    if result.get("ip"):
        update_data = {
            "hosting_ip": result["ip"],
            "hosting_server_id": str(result.get("server_id", "")),
            "hosting_coder_url": result.get("coder_url", ""),
            "hosting_enabled": True,
        }
        if result.get("coder_password"):
            update_data["hosting_coder_password"] = result["coder_password"]
        sb.get_client().table("orgs").update(update_data).eq("slug", slug).execute()

    # Backfill coder_username for all existing active members
    try:
        members = sb.get_client().table("memberships").select(
            "user_id, users!inner(github_username)"
        ).eq("org_slug", slug).eq("status", "active").is_("coder_username", "null").execute()
        for mem in (members.data or []):
            gh_user = mem.get("users", {}).get("github_username", "")
            if gh_user:
                sb.get_client().table("memberships").update(
                    {"coder_username": gh_user}
                ).eq("org_slug", slug).eq("user_id", mem["user_id"]).execute()
        logger.info(f"Backfilled coder_username for {len(members.data or [])} members on {slug}")
    except Exception as e:
        logger.warning(f"coder_username backfill failed for {slug}: {e}")

    logger.info(f"{github_username} enabled hosting for {slug}")
    return {"status": "provisioning", "ip": result.get("ip"), "slug": slug}


@app.get("/api/hosting/status/{slug}")
async def hosting_status(slug: str, authorization: str = Header(...)):
    """Check VPS health + Coder readiness for an org. Any authenticated GitHub user.

    Side effect: when Coder is ready but no session token is stored yet,
    auto-obtains one using the stored coder_password and saves it to Supabase.
    This is how the API gets the admin token after VPS provisioning.
    """
    token = authorization.replace("Bearer ", "").strip()
    try:
        await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    from .services.hosting import get_vps_status
    result = await get_vps_status(slug)

    # Auto-store Coder session token when Coder is ready but token is missing
    if result.get("coder_ready") and result.get("ip"):
        _, token = await _get_coder_credentials(slug)
        if token:
            result["token_stored"] = True

    return result


@app.get("/api/hosting/credentials/{slug}")
async def hosting_credentials(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Get VPS credentials for an org. Admin only. Used for SSH access."""
    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    from .services import supabase as sb
    rows = sb.get_client().table("orgs").select(
        "hosting_ip, hosting_coder_password, hosting_coder_url"
    ).eq("slug", slug).execute()
    if not rows.data:
        raise HTTPException(status_code=404, detail="Org not found")

    ip = rows.data[0].get("hosting_ip", "")
    password = rows.data[0].get("hosting_coder_password", "")
    if not ip or not password:
        raise HTTPException(status_code=404, detail="No hosted VPS or missing credentials")

    logger.info(f"Admin {admin_user} retrieved VPS credentials for {slug}")
    return {"ip": ip, "password": password, "coder_url": rows.data[0].get("hosting_coder_url", "")}


@app.delete("/api/hosting/deprovision/{slug}")
async def hosting_deprovision(slug: str, admin_user: str = Depends(validate_admin_github_token)):
    """Tear down the VPS for an org. Admin only."""
    from .services.hosting import deprovision_vps
    result = await deprovision_vps(slug)

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    # Clear hosting info from Supabase
    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            sb.get_client().table("orgs").update({
                "hosting_ip": None,
                "hosting_server_id": None,
                "hosting_coder_url": None,
                "hosting_coder_token": None,
                "hosting_coder_password": None,
                "hosting_enabled": False,
            }).eq("slug", slug).execute()
        except Exception as e:
            logger.warning(f"Failed to clear hosting info in Supabase: {e}")

    logger.info(f"Admin {admin_user} deprovisioned VPS for {slug}")
    return result


async def _get_coder_credentials(slug: str) -> tuple[str, str]:
    """Get Coder URL and session token for an org, auto-fetching token if missing.

    Returns (coder_url, coder_token). Either may be empty if unavailable.
    """
    if not USE_SUPABASE:
        return "", ""

    from .services import supabase as sb

    rows = sb.get_client().table("orgs").select(
        "hosting_coder_url, hosting_coder_token, hosting_coder_password, hosting_ip"
    ).eq("slug", slug).execute()
    if not rows.data:
        return "", ""

    row = rows.data[0]
    coder_url = row.get("hosting_coder_url", "")
    coder_token = row.get("hosting_coder_token", "")

    # Auto-fetch token if missing but password is available (lazy init)
    if coder_url and not coder_token:
        coder_password = row.get("hosting_coder_password", "")
        ip = row.get("hosting_ip", "")
        if coder_password and ip:
            try:
                from .services.hosting import get_coder_session_token
                session_token = await get_coder_session_token(ip, coder_password)
                if session_token:
                    sb.get_client().table("orgs").update({
                        "hosting_coder_token": session_token,
                    }).eq("slug", slug).execute()
                    coder_token = session_token
                    logger.info(f"Auto-stored Coder session token for {slug}")
            except Exception as e:
                logger.warning(f"Failed to auto-fetch Coder token for {slug}: {e}")

    return coder_url, coder_token


@app.post("/api/hosting/user/{slug}")
async def hosting_create_user(slug: str, body: HostingUser, org: dict = Depends(validate_api_key)):
    """Create a Coder user on an org's VPS. Callable with org API key (used by /invite flow)."""
    from .services.coder import CoderClient

    coder_url, coder_token = await _get_coder_credentials(slug)

    if not coder_url or not coder_token:
        raise HTTPException(status_code=404, detail=f"No hosted Coder instance found for {slug}")

    client = CoderClient(coder_url, coder_token)
    result = await client.create_user(
        username=body.username,
        email=body.email or f"{body.username}@users.noreply.github.com",
        name=body.name or body.username,
    )

    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("detail", "Unknown error"))

    return result


@app.put("/api/hosting/user/{slug}/{username}/roles")
async def hosting_update_user_roles(
    slug: str, username: str, body: dict, org: dict = Depends(validate_api_key)
):
    """Update a Coder user's roles. Requires org API key.
    Uses admin login (not stored token) since role changes need admin privileges."""
    import httpx

    coder_url, coder_password = "", ""
    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            rows = sb.get_client().table("orgs").select(
                "hosting_coder_url, hosting_coder_password, hosting_ip"
            ).eq("slug", slug).execute()
            if rows.data:
                coder_url = rows.data[0].get("hosting_coder_url", "")
                coder_password = rows.data[0].get("hosting_coder_password", "")
                if not coder_url and rows.data[0].get("hosting_ip"):
                    coder_url = f"http://{rows.data[0]['hosting_ip']}"
        except Exception as e:
            logger.warning(f"Failed to look up Coder info: {e}")

    if not coder_url or not coder_password:
        raise HTTPException(status_code=404, detail="No hosted Coder instance found or missing admin password")

    # Login as admin to get a privileged session token
    from .services.hosting import get_coder_session_token
    admin_token = await get_coder_session_token(
        coder_url.replace("http://", "").replace("https://", ""),
        coder_password
    )
    if not admin_token:
        raise HTTPException(status_code=502, detail="Failed to authenticate as Coder admin")

    roles = body.get("roles", [])
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{coder_url}/api/v2/users/{username}/roles",
            headers={
                "Coder-Session-Token": admin_token,
                "Content-Type": "application/json",
            },
            json={"roles": roles},
        )
        if resp.status_code == 200:
            return {"status": "updated", "roles": roles}
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:200])


@app.get("/api/hosting/info/{slug}")
async def hosting_info(slug: str, authorization: str = Header(...)):
    """Get hosting info for an org. Any authenticated user can check if hosting is available.

    Returns coder_url if hosting is enabled, so the website can redirect.
    """
    # Validate the caller has a valid GitHub token
    token = authorization.replace("Bearer ", "").strip()
    try:
        await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    result = {"hosting_enabled": False, "coder_url": None}

    if USE_SUPABASE:
        try:
            from .services import supabase as sb
            rows = sb.get_client().table("orgs").select(
                "hosting_enabled, hosting_coder_url"
            ).eq("slug", slug).execute()
            if rows.data and rows.data[0].get("hosting_enabled"):
                result["hosting_enabled"] = True
                result["coder_url"] = rows.data[0].get("hosting_coder_url", "")
        except Exception as e:
            logger.warning(f"Failed to check hosting info: {e}")

    return result


@app.get("/api/hosting/terminal/{slug}")
async def hosting_terminal_url(slug: str, github_username: str = Depends(validate_github_token)):
    """Return the terminal URL + session token for this user's hosted workspace.

    Flow: user clicks "Open in Browser" on egregore.xyz → frontend calls this →
    we generate a short-lived Coder session token → frontend sets it as a cookie
    on the Coder domain → redirects to terminal. No OAuth app needed on the VPS.
    """
    from .services import supabase as sb
    from .services.coder import CoderClient

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    # Look up org hosting info
    rows = sb.get_client().table("orgs").select(
        "hosting_enabled, hosting_coder_url"
    ).eq("slug", slug).execute()
    if not rows.data or not rows.data[0].get("hosting_enabled"):
        raise HTTPException(status_code=404, detail="Hosting not enabled for this org")

    coder_url = (rows.data[0].get("hosting_coder_url") or "").rstrip("/")
    if not coder_url:
        raise HTTPException(status_code=503, detail="Coder not ready")

    # Look up user's coder_username from their membership
    user = sb.get_user_by_github(github_username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    membership = sb.get_client().table("memberships").select(
        "coder_username"
    ).eq("org_slug", slug).eq("user_id", user["id"]).eq("status", "active").execute()

    coder_username = ""
    if membership.data:
        coder_username = membership.data[0].get("coder_username") or ""
    if not coder_username:
        raise HTTPException(status_code=404, detail="No workspace found for this user")

    # Generate short-lived Coder session token (10 min) — no OAuth app needed
    coder_url_clean, coder_token = await _get_coder_credentials(slug)
    session_token = ""
    if coder_token:
        try:
            coder_client = CoderClient(coder_url, coder_token)
            session_token = await coder_client.create_user_token(coder_username, lifetime_seconds=600)
        except Exception as e:
            logger.warning(f"Failed to create Coder session token for {coder_username}: {e}")

    if not session_token:
        raise HTTPException(status_code=503, detail="Could not generate workspace session")

    terminal_url = f"{coder_url}/@{coder_username}/egregore.main/terminal"

    # Build auth redirect URL — tiny service on VPS port 3200 sets the cookie and redirects
    from urllib.parse import urlencode
    auth_url = f"{coder_url}:3200/auth?{urlencode({'token': session_token, 'redirect': terminal_url})}"

    return {
        "url": auth_url,
        "session_token": session_token,
        "coder_url": coder_url,
    }


@app.post("/api/hosting/workspace/{slug}")
async def hosting_ensure_workspace(slug: str, github_username: str = Depends(validate_github_token)):
    """Ensure a Coder user and workspace exist for this member.

    Called when an existing member wants to open their hosted workspace.
    Creates the Coder user + workspace if they don't exist yet, then
    returns the terminal URL. Idempotent — safe to call multiple times.
    """
    from .services import supabase as sb
    from .services.coder import CoderClient

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    # Verify user is a member of this org
    user = sb.get_user_by_github(github_username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    membership = sb.get_client().table("memberships").select(
        "status, coder_username"
    ).eq("org_slug", slug).eq("user_id", user["id"]).execute()
    if not membership.data or membership.data[0].get("status") != "active":
        raise HTTPException(status_code=403, detail="Not a member of this org")

    # Use existing Coder username if set, otherwise fall back to GitHub username
    coder_username = membership.data[0].get("coder_username") or github_username

    # Get org hosting info
    org_row = sb.get_client().table("orgs").select(
        "hosting_enabled, name, github_org, repo_name, managed_repos"
    ).eq("slug", slug).execute()
    if not org_row.data or not org_row.data[0].get("hosting_enabled"):
        raise HTTPException(status_code=404, detail="Hosting not enabled for this org")

    org = org_row.data[0]

    coder_url, coder_token = await _get_coder_credentials(slug)
    if not coder_url:
        raise HTTPException(status_code=503, detail="Coder not ready")
    if not coder_token:
        raise HTTPException(status_code=503, detail="Cannot authenticate with Coder")

    coder_client = CoderClient(coder_url, coder_token)

    # Create user if needed (uses coder_username — may differ from GitHub username)
    await coder_client.create_user(
        username=coder_username,
        email=f"{github_username}@users.noreply.github.com",
        name=user.get("display_name") or user.get("name") or github_username,
    )

    # Create workspace if needed (with org parameters)
    ws_result = await coder_client.create_workspace(
        owner=coder_username,
        org_slug=slug,
        org_name=org.get("name", slug),
        github_org=org.get("github_org", ""),
        repo_name=org.get("repo_name", ""),
        managed_repos=org.get("managed_repos", ""),
    )

    # Store coder_username on membership if not already set
    if not membership.data[0].get("coder_username"):
        try:
            sb.get_client().table("memberships").update(
                {"coder_username": coder_username}
            ).eq("org_slug", slug).eq("user_id", user["id"]).execute()
        except Exception:
            pass

    terminal_url = f"{coder_url}/@{coder_username}/egregore.main/terminal"

    # Generate short-lived session token so user doesn't need Coder OAuth
    session_token = ""
    try:
        session_token = await coder_client.create_user_token(coder_username, lifetime_seconds=600)
    except Exception as e:
        logger.warning(f"Failed to create Coder session token for {coder_username}: {e}")

    # Build auth redirect URL — tiny service on VPS port 3200 sets the cookie and redirects
    auth_url = ""
    if session_token:
        from urllib.parse import urlencode
        auth_url = f"{coder_url}:3200/auth?{urlencode({'token': session_token, 'redirect': terminal_url})}"

    return {
        "status": ws_result.get("status", "error"),
        "terminal_url": auth_url or terminal_url,
        "coder_url": coder_url,
        "session_token": session_token,
    }


@app.get("/api/hosting/workspace-status/{slug}")
async def hosting_workspace_status(
    slug: str,
    github_username: str = Depends(validate_github_token),
):
    """Check if the user's workspace is ready (agent connected)."""
    from .services import supabase as sb

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    # Look up coder_username from membership (may differ from GitHub username)
    user = sb.get_user_by_github(github_username)
    coder_username = github_username
    if user:
        mem = sb.get_client().table("memberships").select(
            "coder_username"
        ).eq("org_slug", slug).eq("user_id", user["id"]).execute()
        if mem.data and mem.data[0].get("coder_username"):
            coder_username = mem.data[0]["coder_username"]

    rows = sb.get_client().table("orgs").select(
        "hosting_enabled"
    ).eq("slug", slug).execute()
    if not rows.data or not rows.data[0].get("hosting_enabled"):
        raise HTTPException(status_code=404, detail="Hosting not enabled")

    coder_url, coder_token = await _get_coder_credentials(slug)
    if not coder_token:
        raise HTTPException(status_code=503, detail="Cannot authenticate with Coder")

    coder_client = CoderClient(coder_url, coder_token)
    status = await coder_client.get_workspace_status(owner=coder_username)
    terminal_url = f"{coder_url}/@{coder_username}/egregore.main/terminal"

    return {
        **status,
        "terminal_url": terminal_url,
    }


# =============================================================================
# USER API KEYS
# =============================================================================


@app.get("/api/user/keys")
async def user_keys_status(authorization: str = Header(...)):
    """Check which API keys a user has set (without decrypting).

    Requires GitHub token. Returns key status for the authenticated user.
    """
    token = authorization.replace("Bearer ", "").strip()
    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    from .services.keys import get_user_key_status
    return get_user_key_status(user["login"])


@app.put("/api/user/keys")
async def user_keys_update(body: UserKeysUpdate, authorization: str = Header(...)):
    """Store or update a user's API key. Encrypted at rest.

    Requires GitHub token. Only the authenticated user can set their own keys.
    """
    token = authorization.replace("Bearer ", "").strip()
    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    from .services.keys import store_user_key

    results = {}
    if body.anthropic_api_key is not None:
        ok = store_user_key(user["login"], "anthropic_api_key", body.anthropic_api_key)
        results["anthropic_api_key"] = "stored" if ok else "failed"

    if not results:
        raise HTTPException(status_code=400, detail="No keys provided")

    return {"status": "ok", **results}


@app.delete("/api/user/keys/{key_name}")
async def user_keys_delete(key_name: str, authorization: str = Header(...)):
    """Delete a stored API key.

    Requires GitHub token. Only the authenticated user can delete their own keys.
    """
    token = authorization.replace("Bearer ", "").strip()
    try:
        user = await gh.get_user(token)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid GitHub token")

    allowed_keys = {"anthropic_api_key"}
    if key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Unknown key: {key_name}")

    from .services.keys import delete_user_key
    delete_user_key(user["login"], key_name)
    return {"status": "ok", "deleted": key_name}


@app.get("/api/hosting/workspace-config/{slug}")
async def hosting_workspace_config(
    slug: str,
    username: str = Query(...),
    org: dict = Depends(validate_api_key),
):
    """Return all config a workspace needs to initialize.

    Called once by workspace-init.py on startup. Single call replaces
    multiple bash operations that used to fail silently.
    """
    from .services import supabase as sb

    if not USE_SUPABASE:
        raise HTTPException(status_code=501, detail="Requires Supabase")

    # Get org info
    org_row = sb.get_client().table("orgs").select(
        "slug, name, github_org, hosting_enabled, hosting_coder_url"
    ).eq("slug", slug).execute()
    if not org_row.data:
        raise HTTPException(status_code=404, detail="Org not found")
    org_data = org_row.data[0]

    # Get memory repo URL from egregore.json config (stored as template var)
    # We return it from the org table or fall back to convention
    memory_repo = f"https://github.com/{org_data.get('github_org', '')}/{slug}-memory.git"

    # Get user's Anthropic key if they have one
    anthropic_key = ""
    try:
        from .services.keys import get_user_key
        anthropic_key = get_user_key(username, "anthropic_api_key") or ""
    except Exception:
        pass

    # Get user's membership info
    user = sb.get_user_by_github(username)
    display_name = ""
    if user:
        membership = sb.get_client().table("memberships").select(
            "display_name, member_role, coder_username"
        ).eq("org_slug", slug).eq("user_id", user["id"]).execute()
        if membership.data:
            display_name = membership.data[0].get("display_name") or ""

    return {
        "egregore_json": {
            "org_name": org_data.get("name", slug),
            "github_org": org_data.get("github_org", ""),
            "memory_repo": memory_repo,
            "api_url": os.environ.get("API_URL", "https://egregore-production-55f2.up.railway.app"),
            "slug": slug,
            "repos": [],
        },
        "env_vars": {
            "ANTHROPIC_API_KEY": anthropic_key,
        },
        "state": {
            "org_setup": True,
            "github_username": username,
            "display_name": display_name or username,
            "onboarding_complete": False,
            "workspace_ready": True,
        },
    }


@app.get("/api/user/keys/fetch")
async def user_keys_fetch(
    key_name: str = Query(...),
    github_username: str = Query(...),
    org: dict = Depends(validate_api_key),
):
    """Fetch a decrypted user key. For workspace init only — requires org API key + github_username.

    Used by workspace-init.sh to inject the user's Anthropic key into the workspace
    without them re-entering it.
    """

    allowed_keys = {"anthropic_api_key"}
    if key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Unknown key: {key_name}")

    from .services.keys import get_user_key
    value = get_user_key(github_username, key_name)
    if not value:
        return {"status": "not_set", "value": None}
    return {"status": "ok", "value": value}


# =============================================================================
# GOOGLE CONNECTOR ENDPOINTS
# =============================================================================


@app.get("/api/connectors/google/credentials")
async def google_credentials(org: dict = Depends(validate_api_key)):
    """Return Google OAuth Client ID + Secret for local connector auth.

    Credentials are shared across all orgs — they identify the Egregore app,
    not the user or org. Each user authenticates with their own Google account.
    Self-hosted orgs can override via GOOGLE_CLIENT_ID/SECRET in their .env.
    """
    from .services.google import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="Google OAuth credentials not configured on server")
    return {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET}


@app.get("/api/connectors/google/auth-url")
async def google_auth_url(org: dict = Depends(validate_api_key)):
    """Get Google OAuth consent URL (for hosted deployments)."""
    from .services.google import get_auth_url
    try:
        url = get_auth_url(state=org.get("slug", ""))
        return {"url": url}
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/connectors/google/callback")
async def google_callback(
    body: GoogleOAuthCallback,
    org: dict = Depends(validate_api_key),
):
    """Exchange Google OAuth code for tokens. Store encrypted in Supabase."""
    from .services.google import exchange_code
    from .services.keys import store_user_key

    result = await exchange_code(body.code)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # Store tokens encrypted
    if result.get("access_token"):
        store_user_key(body.github_username, "google_oauth_token", result["access_token"])
    if result.get("refresh_token"):
        store_user_key(body.github_username, "google_refresh_token", result["refresh_token"])

    # Update user record with Google email
    if USE_SUPABASE and result.get("email"):
        try:
            from .services.supabase import get_client
            get_client().table("users").update({
                "google_account_email": result["email"],
                "google_oauth_token_set": True,
            }).eq("github_username", body.github_username).execute()
        except Exception:
            pass  # Non-fatal

    return {
        "status": "ok",
        "email": result.get("email", ""),
    }


@app.get("/api/connectors/google/status")
async def google_status(
    github_username: str = Query(...),
    org: dict = Depends(validate_api_key),
):
    """Check if a user has valid Google tokens."""
    if not USE_SUPABASE:
        return {"connected": False, "reason": "supabase disabled"}

    try:
        from .services.supabase import get_client
        result = get_client().table("users").select(
            "google_oauth_token_set, google_account_email"
        ).eq("github_username", github_username).limit(1).execute()

        if not result.data:
            return {"connected": False}

        row = result.data[0]
        return {
            "connected": bool(row.get("google_oauth_token_set")),
            "email": row.get("google_account_email", ""),
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


@app.post("/api/connectors/google/revoke")
async def google_revoke(
    github_username: str = Query(...),
    org: dict = Depends(validate_api_key),
):
    """Revoke Google tokens for a user."""
    from .services.keys import get_user_key, delete_user_key
    from .services.google import revoke_token

    # Try to revoke the token with Google
    token = get_user_key(github_username, "google_oauth_token")
    if token:
        await revoke_token(token)

    # Delete stored tokens
    delete_user_key(github_username, "google_oauth_token")
    delete_user_key(github_username, "google_refresh_token")

    # Clear Google status in user record
    if USE_SUPABASE:
        try:
            from .services.supabase import get_client
            get_client().table("users").update({
                "google_oauth_token_set": False,
                "google_account_email": None,
            }).eq("github_username", github_username).execute()
        except Exception:
            pass

    return {"status": "revoked"}


@app.post("/api/connectors/google/promote")
async def google_promote(
    body: GooglePromote,
    org: dict = Depends(validate_api_key),
):
    """Promote Google content to shared memory — create Artifact node + connections."""
    import uuid

    # Build Cypher queries for the graph
    queries = []

    # 1. Core Artifact node (MERGE on google_id for dedup)
    queries.append({
        "statement": """
            MERGE (a:Artifact {google_id: $googleId})
            ON CREATE SET
                a.id = $id,
                a.title = $title,
                a.type = 'source',
                a.origin = $origin,
                a.created = date(),
                a.filePath = $filePath,
                a.summary = $summary,
                a.topics = $topics
            ON MATCH SET
                a.summary = $summary,
                a.topics = $topics,
                a.updated = date()
            RETURN a.id AS artifact_id
        """,
        "parameters": {
            "googleId": body.google_id,
            "id": str(uuid.uuid4()),
            "title": body.title,
            "origin": f"google-{body.service}",
            "filePath": body.file_path,
            "summary": body.summary,
            "topics": body.topics,
        },
    })

    # 2. Author link
    queries.append({
        "statement": """
            MATCH (a:Artifact {google_id: $googleId}), (p:Person {github: $author})
            MERGE (a)-[:CONTRIBUTED_BY]->(p)
        """,
        "parameters": {
            "googleId": body.google_id,
            "author": body.github_username,
        },
    })

    # 3. Mentioned people
    if body.mentioned_people:
        queries.append({
            "statement": """
                MATCH (a:Artifact {google_id: $googleId})
                UNWIND $mentions AS personName
                MATCH (p:Person {name: personName})
                MERGE (a)-[:MENTIONS]->(p)
            """,
            "parameters": {
                "googleId": body.google_id,
                "mentions": body.mentioned_people,
            },
        })

    # 4. Quest connections
    if body.related_quests:
        queries.append({
            "statement": """
                MATCH (a:Artifact {google_id: $googleId})
                UNWIND $quests AS questName
                MATCH (q:Quest {name: questName})
                MERGE (a)-[:RELATES_TO]->(q)
            """,
            "parameters": {
                "googleId": body.google_id,
                "quests": body.related_quests,
            },
        })

    # Execute all queries
    results = []
    for q in queries:
        try:
            result = await execute_query(org, q["statement"], q["parameters"])
            results.append(result)
        except Exception as e:
            logger.warning(f"Graph query failed during promotion: {e}")
            results.append({"error": str(e)})

    # Extract artifact ID from the first query result
    artifact_id = None
    if results and isinstance(results[0], dict):
        values = results[0].get("values", [])
        if values:
            artifact_id = values[0][0] if isinstance(values[0], list) else values[0]

    return {
        "status": "promoted",
        "artifact_id": artifact_id,
        "graph_queries": len(queries),
        "graph_results": len(results),
    }


# =============================================================================
# HEALTH
# =============================================================================


@app.get("/health")
async def health():
    return {"status": "ok", "service": "egregore-api", "supabase": USE_SUPABASE}


@app.get("/api/admin/debug")
async def admin_debug(admin_user: str = Depends(validate_admin_github_token)):
    """Temporary: test each Supabase call individually to find the crash."""
    if not USE_SUPABASE:
        return {"error": "no supabase"}
    from .services import supabase as sb
    results = {}
    for name, fn in [
        ("list_orgs", lambda: sb.list_orgs()),
        ("list_api_keys", lambda: sb.list_api_keys()),
        ("get_all_memberships", lambda: sb.get_all_memberships()),
        ("get_telemetry_events", lambda: sb.get_telemetry_events(limit=5)),
    ]:
        try:
            data = fn()
            results[name] = {"ok": True, "count": len(data)}
        except Exception as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # Also test Neo4j
    seed_org = _get_seed_org()
    if seed_org:
        try:
            r = await execute_system_query(seed_org, "MATCH (s:Session) WITH s.org AS org, count(s) AS cnt RETURN org, cnt")
            results["neo4j_sessions"] = {"ok": True, "rows": len(r.get("values", []))}
        except Exception as e:
            results["neo4j_sessions"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return results


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api.main:app", host="0.0.0.0", port=port, reload=True)
