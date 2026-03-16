"""
Egregore Bot

A Telegram bot that serves as the voice of an egregore — answering
questions about team activity, decisions, and knowledge by querying
the Neo4j graph and searching the shared memory repo.

Uses LLM (Sonnet) with tool use to route questions to the right
data source and format responses conversationally.

Deploy to Railway with webhook mode for production.
"""

import os
import json
import logging
import time
import re
import subprocess
import asyncio
from datetime import date
from pathlib import Path
from typing import Optional

from analytics import log_query_event, log_event
import secrets
import hashlib
import uuid

from dotenv import load_dotenv
load_dotenv(override=False)

import httpx
from neo4j import GraphDatabase

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
import uvicorn

# =============================================================================
# CONFIG
# =============================================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", 8443))

# Neo4j Aura (default/legacy - used if no org match)
NEO4J_URI = os.environ.get("NEO4J_URI", "")
NEO4J_USER = os.environ.get("NEO4J_USER", "")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

# GitHub token for adding collaborators
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Anthropic LLM
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-20250514")

# Spirit adapter admin secret (required for /spirit/init)
SPIRIT_ADMIN_SECRET = os.environ.get("SPIRIT_ADMIN_SECRET", "")

# Egregore API (for dynamic org registration)
EGREGORE_API_URL = os.environ.get("EGREGORE_API_URL", "")

# Shared customer Neo4j — derive URI from HOST if URI not set directly.
# The API uses EGREGORE_NEO4J_HOST (hostname only), bot needs full URI for Python driver.
EGREGORE_NEO4J_URI = os.environ.get("EGREGORE_NEO4J_URI", "")
EGREGORE_NEO4J_USER = os.environ.get("EGREGORE_NEO4J_USER", "neo4j")
EGREGORE_NEO4J_PASSWORD = os.environ.get("EGREGORE_NEO4J_PASSWORD", "")
if not EGREGORE_NEO4J_URI:
    _host = os.environ.get("EGREGORE_NEO4J_HOST", "")
    if _host:
        EGREGORE_NEO4J_URI = f"neo4j+s://{_host}"

# Security: Allowed chats/users — loaded dynamically from Neo4j + ORG_CONFIG at startup.
# Env override still works for manual additions.
ALLOWED_CHAT_IDS = [
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
]

# =============================================================================
# MULTI-ORG CONFIG
# =============================================================================

EGREGORE_CHANNEL_ID = int(os.environ.get("EGREGORE_CHANNEL_ID", "0") or "0")

ORG_CONFIG = {
    -1003527692267: {
        "name": "curvelabs",
        "neo4j_uri": os.environ.get("NEO4J_URI", ""),
        "neo4j_user": os.environ.get("NEO4J_USER", "neo4j"),
        "neo4j_password": os.environ.get("NEO4J_PASSWORD", ""),
        "github_org": "Curve-Labs",
        "_static": True,
    },
}

# Egregore org (new standalone org)
if EGREGORE_CHANNEL_ID:
    ORG_CONFIG[EGREGORE_CHANNEL_ID] = {
        "name": "egregore",
        "neo4j_uri": EGREGORE_NEO4J_URI,
        "neo4j_user": EGREGORE_NEO4J_USER,
        "neo4j_password": EGREGORE_NEO4J_PASSWORD,
    }


def load_dynamic_orgs():
    """Load org configs + allowed users on startup.

    Tries API first (/api/internal/orgs), falls back to Neo4j.
    Populates ORG_CONFIG with orgs that have telegram_chat_id set.
    Populates ALLOWED_CHAT_IDS with user telegram IDs.
    """
    # Try loading from API first (Supabase-backed when USE_SUPABASE=true)
    if EGREGORE_API_URL:
        try:
            headers = {}
            if BOT_TOKEN:
                headers["Authorization"] = f"Bearer {BOT_TOKEN}"
            resp = httpx.get(
                f"{EGREGORE_API_URL}/api/internal/orgs",
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                org_count = 0
                for org in data.get("orgs", []):
                    chat_id_str = org.get("telegram_chat_id")
                    if not chat_id_str:
                        continue
                    chat_id = int(chat_id_str)
                    slug = org.get("slug", org.get("name", ""))

                    if chat_id in ORG_CONFIG and ORG_CONFIG[chat_id].get("_static"):
                        continue  # Don't overwrite hardcoded static config

                    # Derive Neo4j URI from host if needed
                    neo4j_host = org.get("neo4j_host", "")
                    neo4j_uri = f"neo4j+s://{neo4j_host}" if neo4j_host else EGREGORE_NEO4J_URI

                    ORG_CONFIG[chat_id] = {
                        "name": slug,
                        "neo4j_uri": neo4j_uri,
                        "neo4j_user": org.get("neo4j_user", "neo4j"),
                        "neo4j_password": org.get("neo4j_password", ""),
                        "github_org": org.get("github_org", ""),
                    }
                    if chat_id not in ALLOWED_CHAT_IDS:
                        ALLOWED_CHAT_IDS.append(chat_id)
                    org_count += 1

                # Also add all org chat_ids from static ORG_CONFIG
                for cid in ORG_CONFIG:
                    if cid not in ALLOWED_CHAT_IDS:
                        ALLOWED_CHAT_IDS.append(cid)

                logger.info(f"Loaded {org_count} org(s) from API")

                # Still load allowed users from Neo4j (Person nodes with telegramId)
                _load_allowed_users_from_neo4j()
                return
            else:
                logger.warning(f"API org load failed ({resp.status_code}), falling back to Neo4j")
        except Exception as e:
            logger.warning(f"API org load failed: {e}, falling back to Neo4j")

    # Fallback: load from Neo4j directly
    _load_orgs_from_neo4j()


def _load_allowed_users_from_neo4j():
    """Load allowed user IDs from Person nodes in Neo4j."""
    shared_uri = EGREGORE_NEO4J_URI or NEO4J_URI
    shared_user = EGREGORE_NEO4J_USER if EGREGORE_NEO4J_URI else NEO4J_USER
    shared_password = EGREGORE_NEO4J_PASSWORD if EGREGORE_NEO4J_URI else NEO4J_PASSWORD

    if not shared_uri or not shared_password:
        return

    try:
        driver = GraphDatabase.driver(shared_uri, auth=(shared_user, shared_password))
        with driver.session() as session:
            people = session.run(
                "MATCH (p:Person) WHERE p.telegramId IS NOT NULL RETURN p.telegramId AS tid"
            )
            user_count = 0
            for record in people:
                tid = int(record["tid"])
                if tid not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(tid)
                    user_count += 1
            logger.info(f"Loaded {user_count} allowed user(s) from Neo4j")
        driver.close()
    except Exception as e:
        logger.error(f"Failed to load allowed users from Neo4j: {e}")


def _load_orgs_from_neo4j():
    """Original Neo4j-based org loading (fallback)."""
    shared_uri = EGREGORE_NEO4J_URI
    shared_user = EGREGORE_NEO4J_USER
    shared_password = EGREGORE_NEO4J_PASSWORD

    if not shared_uri or not shared_password:
        if NEO4J_URI and NEO4J_PASSWORD:
            shared_uri = NEO4J_URI
            shared_user = NEO4J_USER
            shared_password = NEO4J_PASSWORD
        else:
            logger.info("No Neo4j configured — skipping dynamic loading")
            return

    try:
        driver = GraphDatabase.driver(shared_uri, auth=(shared_user, shared_password))
        with driver.session() as session:
            result = session.run(
                "MATCH (o:Org) WHERE o.telegram_chat_id IS NOT NULL "
                "RETURN o.id AS id, o.name AS name, o.telegram_chat_id AS chat_id, o.api_key AS api_key"
            )
            org_count = 0
            for record in result:
                chat_id = int(record["chat_id"])
                org_id = record["id"] or record["name"]
                if chat_id in ORG_CONFIG:
                    existing = ORG_CONFIG[chat_id]
                    if existing.get("_static"):
                        continue
                    logger.info(f"Replacing org {existing.get('name')} with {org_id} for chat {chat_id}")
                ORG_CONFIG[chat_id] = {
                    "name": org_id,
                    "neo4j_uri": shared_uri,
                    "neo4j_user": shared_user,
                    "neo4j_password": shared_password,
                }
                if chat_id not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(chat_id)
                org_count += 1

            people = session.run(
                "MATCH (p:Person) WHERE p.telegramId IS NOT NULL RETURN p.telegramId AS tid"
            )
            user_count = 0
            for record in people:
                tid = int(record["tid"])
                if tid not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(tid)
                    user_count += 1

            for cid in ORG_CONFIG:
                if cid not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(cid)

            logger.info(f"Loaded {org_count} org(s), {user_count} user(s) from Neo4j")
        driver.close()
    except Exception as e:
        logger.error(f"Failed to load dynamic orgs: {e}")


async def register_group(slug: str, chat_id: int, group_title: str = None, group_username: str = None) -> dict | None:
    """Register a Telegram group with the API."""
    if not EGREGORE_API_URL:
        logger.error("No EGREGORE_API_URL configured — cannot register group")
        return None

    headers = {"Content-Type": "application/json"}
    if BOT_TOKEN:
        headers["Authorization"] = f"Bearer {BOT_TOKEN}"

    body = {"org_slug": slug, "chat_id": str(chat_id)}
    if group_title:
        body["group_title"] = group_title
    if group_username:
        body["group_username"] = group_username

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{EGREGORE_API_URL}/api/org/telegram",
                json=body,
                headers=headers,
                timeout=10.0,
            )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"Registered group {chat_id} for org {slug} via API")
            return data
        else:
            logger.error(f"API registration failed: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        logger.error(f"API registration failed: {e}")
        return None


# Load dynamic orgs at import time (after static config)
load_dynamic_orgs()


def get_org_for_chat(chat_id: int, user_id: int = None) -> dict:
    """Get org config for a chat."""
    if chat_id in ORG_CONFIG:
        return ORG_CONFIG[chat_id]
    return {
        "name": "default",
        "neo4j_uri": NEO4J_URI,
        "neo4j_user": NEO4J_USER,
        "neo4j_password": NEO4J_PASSWORD,
    }

# Log startup config
logger.info("=== Egregore Bot Starting ===")
logger.info(f"NEO4J_URI: {'SET' if NEO4J_URI else 'NOT SET'}")
logger.info(f"EGREGORE_NEO4J_URI: {'SET' if EGREGORE_NEO4J_URI else 'NOT SET'} (derived from {'EGREGORE_NEO4J_URI' if os.environ.get('EGREGORE_NEO4J_URI') else 'EGREGORE_NEO4J_HOST' if os.environ.get('EGREGORE_NEO4J_HOST') else 'NONE'})")
logger.info(f"ANTHROPIC_API_KEY: {'SET' if ANTHROPIC_API_KEY else 'NOT SET'}")
logger.info(f"ALLOWED_CHAT_IDS: {ALLOWED_CHAT_IDS}")

# =============================================================================
# NEO4J CONNECTION
# =============================================================================

neo4j_driver = None
org_drivers = {}  # Cache of org-specific drivers

def get_neo4j_driver():
    """Get or create default Neo4j driver."""
    global neo4j_driver
    if neo4j_driver is None and NEO4J_URI:
        neo4j_driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        logger.info("Neo4j driver initialized (default)")
    return neo4j_driver


def get_org_driver(org_config: dict):
    """Get or create org-specific Neo4j driver."""
    if not org_config:
        return get_neo4j_driver()

    org_name = org_config.get("name", "default")
    uri = org_config.get("neo4j_uri")
    user = org_config.get("neo4j_user", "neo4j")
    password = org_config.get("neo4j_password")

    if not uri or not password:
        logger.warning(f"Neo4j not configured for org {org_name}")
        return None

    if org_name not in org_drivers:
        org_drivers[org_name] = GraphDatabase.driver(uri, auth=(user, password))
        logger.info(f"Neo4j driver initialized for org: {org_name}")

    return org_drivers[org_name]


def run_query(query: str, params: dict = None) -> list:
    """Run a Cypher query on default Neo4j."""
    driver = get_neo4j_driver()
    if not driver:
        logger.warning("Neo4j not configured")
        return []

    try:
        with driver.session() as session:
            result = session.run(query, params or {})
            return [dict(record) for record in result]
    except Exception as e:
        logger.error(f"Neo4j query failed: {e}")
        return []


def inject_org_scope(statement: str, org_slug: str) -> str:
    """Inject org property into ALL labeled node patterns.

    Same logic as api/services/graph.py — ensures tenant isolation.
    Transforms:
      (p:Person {name: $name}) → (p:Person {name: $name, org: $_org})
      (s:Session)              → (s:Session {org: $_org})
    Skips: CALL statements, nodes with org already set, system labels.
    """
    if statement.strip().upper().startswith("CALL"):
        return statement

    SYSTEM_LABELS = {"TelegramUser", "Org"}

    def add_org_to_props(match):
        full = match.group(0)
        if "org:" in full or "org :" in full:
            return full
        label_match = re.search(r':([A-Z]\w*)', full)
        if label_match and label_match.group(1) in SYSTEM_LABELS:
            return full
        return full.rstrip("}") + ", org: $_org}"

    pattern_with_props = r'\([a-zA-Z_]\w*:[A-Z]\w*\s*\{[^}]*\}'
    result = re.sub(pattern_with_props, add_org_to_props, statement)

    def add_org_to_bare(match):
        var = match.group(1)
        label = match.group(2)
        if label in SYSTEM_LABELS:
            return match.group(0)
        return f"({var}:{label} {{org: $_org}})"

    result = re.sub(
        r'\(([a-zA-Z_]\w*):([A-Z]\w*)\)(?!\s*\{)',
        add_org_to_bare,
        result,
    )

    return result


def run_org_query(query: str, params: dict = None, org_config: dict = None) -> list:
    """Run a Cypher query on org-specific Neo4j with org scoping."""
    driver = get_org_driver(org_config)
    if not driver:
        logger.warning("Neo4j not configured for org")
        return []

    # Inject org scoping for tenant isolation
    org_slug = org_config.get("name") if org_config else None
    if org_slug:
        query = inject_org_scope(query, org_slug)
        params = dict(params or {})
        params["_org"] = org_slug

    try:
        with driver.session() as session:
            result = session.run(query, params or {})
            return [dict(record) for record in result]
    except Exception as e:
        logger.error(f"Neo4j org query failed: {e}")
        return []


def lookup_person_by_telegram_id(telegram_id: int, org_config: dict = None) -> Optional[str]:
    """Look up Person name by Telegram ID.

    Uses org-specific driver if provided (Person nodes are per-org DB).
    Falls back to default driver for backwards compatibility.
    """
    query = "MATCH (p:Person {telegramId: $tid}) RETURN p.name AS name"
    params = {"tid": telegram_id}
    results = run_org_query(query, params, org_config) if org_config else run_query(query, params)
    if results and results[0].get("name"):
        return results[0]["name"]
    return None


def auto_register_telegram_id(telegram_id: int, first_name: str = None, username: str = None, org_config: dict = None) -> Optional[str]:
    """Auto-register a Telegram ID to a Person node if we can match them.

    Person queries use org-specific driver (Person nodes live in the org's DB).
    TelegramUser queries use default driver (TelegramUser is global/unscoped).
    """
    _run_person = (lambda q, p: run_org_query(q, p, org_config)) if org_config else run_query

    # First check if already registered
    existing = lookup_person_by_telegram_id(telegram_id, org_config)
    if existing:
        # Update username on existing TelegramUser (global DB)
        if username:
            run_query(
                "MATCH (tu:TelegramUser {telegramId: $tid}) SET tu.username = $username",
                {"tid": telegram_id, "username": username},
            )
        return existing

    # Try matching by Telegram username → Person.telegramUsername (set by API)
    if username:
        results = _run_person(
            """MATCH (p:Person {telegramUsername: $username})
               WHERE p.telegramId IS NULL
               SET p.telegramId = $tid
               RETURN p.name AS name""",
            {"username": username, "tid": telegram_id}
        )
        if results:
            person_name = results[0].get("name")
            logger.info(f"Auto-registered {person_name} with Telegram ID {telegram_id} (matched by username)")
            # TelegramUser + IDENTIFIES: only works if both nodes are in same DB.
            # For CL (same DB), create the link. For customer orgs (split DB), skip.
            run_query(
                """
                MERGE (tu:TelegramUser {telegramId: $tid})
                SET tu.firstName = $firstName, tu.username = $username
                """,
                {"tid": telegram_id, "firstName": first_name or "", "username": username},
            )
            return person_name

    # Try matching by first name (lowercase)
    if first_name:
        name_lower = first_name.lower().strip()
        results = _run_person(
            """MATCH (p:Person)
               WHERE (p.name = $name OR toLower(p.fullName) STARTS WITH $name)
               AND p.telegramId IS NULL
               SET p.telegramId = $tid
               RETURN p.name AS name""",
            {"name": name_lower, "tid": telegram_id}
        )
        if results:
            person_name = results[0].get("name")
            logger.info(f"Auto-registered {person_name} with Telegram ID {telegram_id} (matched by first name)")
            run_query(
                """
                MERGE (tu:TelegramUser {telegramId: $tid})
                SET tu.firstName = $firstName, tu.username = $username
                """,
                {"tid": telegram_id, "firstName": first_name, "username": username or ""},
            )
            return person_name

    return None


async def _sync_telegram_user_to_supabase(api_key: str, identifier: str, first_name: str, username: str, telegram_id: int):
    """Ensure Telegram user exists in Supabase as a member. Best-effort."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{EGREGORE_API_URL}/api/user/ensure",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "github_username": identifier,
                    "github_name": first_name or identifier,
                    "telegram_username": username or None,
                    "telegram_id": telegram_id,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"Supabase sync OK: {identifier} (tg:{username})")
            else:
                logger.warning(f"Supabase sync failed for {identifier}: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Supabase sync error for {identifier}: {e}")


def track_telegram_membership(telegram_id: int, username: str, first_name: str, org_slug: str, action: str = "join"):
    """Track Telegram group membership via TelegramUser nodes.

    TelegramUser + IN_GROUP: default driver (global/unscoped).
    Person linking (telegramId, IDENTIFIES): org-specific driver (Person is per-org DB).

    action: "join" or "leave"
    """
    driver = get_neo4j_driver()
    if not driver:
        logger.warning("Neo4j not configured — cannot track membership")
        return

    # Get org-specific driver for Person operations
    org_config = None
    for cid, cfg in ORG_CONFIG.items():
        if cfg.get("name") == org_slug:
            org_config = cfg
            break
    org_driver = get_org_driver(org_config) if org_config else driver

    try:
        # TelegramUser + IN_GROUP — use org driver (Org node lives in org's DB)
        with org_driver.session() as session:
            if action == "join":
                session.run(
                    """
                    MERGE (tu:TelegramUser {telegramId: $tid})
                    SET tu.username = $username, tu.firstName = $firstName
                    WITH tu
                    MATCH (o:Org {id: $org})
                    MERGE (tu)-[r:IN_GROUP]->(o)
                    SET r.joinedAt = datetime(), r.status = 'active'
                    """,
                    {"tid": telegram_id, "username": username or "", "firstName": first_name or "", "org": org_slug},
                )
            elif action == "leave":
                session.run(
                    """
                    MATCH (tu:TelegramUser {telegramId: $tid})-[r:IN_GROUP]->(o:Org {id: $org})
                    SET r.status = 'left', r.leftAt = datetime()
                    """,
                    {"tid": telegram_id, "org": org_slug},
                )
                logger.info(f"Tracked leave: TelegramUser {telegram_id} from org {org_slug}")
                return

        # Person linking — use run_org_query for org scoping
        if username:
            run_org_query(
                """
                MATCH (p:Person)
                WHERE p.telegramUsername = $username
                AND p.telegramId IS NULL
                SET p.telegramId = $tid
                """,
                {"tid": telegram_id, "username": username},
                org_config,
            )

        logger.info(f"Tracked join: TelegramUser {telegram_id} ({first_name}) → org {org_slug}")

        # Sync to Supabase — ensure user + membership exist
        if EGREGORE_API_URL and org_config:
            try:
                api_key = org_config.get("api_key", "")
                identifier = username or first_name or str(telegram_id)
                import asyncio
                loop = asyncio.get_event_loop()
                loop.create_task(_sync_telegram_user_to_supabase(
                    api_key, identifier, first_name, username, telegram_id
                ))
            except Exception as se:
                logger.warning(f"Supabase sync failed for {first_name}: {se}")
    except Exception as e:
        logger.error(f"Failed to track membership: {e}")


def get_org_team_names(org_config: dict = None) -> list:
    """Fetch team member names for an org from Neo4j."""
    if not org_config:
        return []
    org_slug = org_config.get("name")
    if not org_slug:
        return []
    results = run_org_query(
        "MATCH (p:Person) RETURN p.name AS name ORDER BY p.name",
        {},
        org_config,
    )
    return [r["name"] for r in results if r.get("name")]


# =============================================================================
# CAPABILITY FLAGS
# =============================================================================

GRAPH_AVAILABLE = False  # Set during startup
MEMORY_AVAILABLE = False  # Set after memory clone attempt


def check_graph_availability():
    """Test if any Neo4j connection works."""
    global GRAPH_AVAILABLE
    for cid, cfg in ORG_CONFIG.items():
        try:
            driver = get_org_driver(cfg)
            if driver:
                with driver.session() as session:
                    session.run("RETURN 1")
                GRAPH_AVAILABLE = True
                logger.info("Graph availability: OK")
                return
        except Exception:
            continue
    logger.warning("Graph availability: NONE — running in memory-only mode")


# =============================================================================
# MEMORY REPO
# =============================================================================

MEMORY_BASE_DIR = Path(os.environ.get("MEMORY_DIR", "/app/memory"))
MEMORY_REPOS: dict[str, Path] = {}  # org_slug -> local path
ORG_IDENTITIES: dict[str, str] = {}  # org_slug -> identity text (cached)


def clone_memory_repo(org_slug: str, github_org: str) -> Optional[Path]:
    """Clone or update memory repo for an org. Returns local path or None."""
    if not GITHUB_TOKEN or not github_org:
        return None

    repo_dir = MEMORY_BASE_DIR / org_slug
    repo_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{github_org}/{github_org}-memory.git"

    try:
        if repo_dir.exists() and (repo_dir / ".git").exists():
            subprocess.run(
                ["git", "-C", str(repo_dir), "pull", "--quiet"],
                capture_output=True, timeout=30
            )
            logger.info(f"Memory repo updated: {org_slug}")
        else:
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", repo_url, str(repo_dir)],
                capture_output=True, timeout=60, check=True
            )
            logger.info(f"Memory repo cloned: {org_slug}")

        MEMORY_REPOS[org_slug] = repo_dir
        return repo_dir
    except Exception as e:
        logger.warning(f"Memory repo clone/pull failed for {org_slug}: {e}")
        return None


def init_memory_repos():
    """Clone memory repos for all configured orgs on startup."""
    global MEMORY_AVAILABLE
    for cid, cfg in ORG_CONFIG.items():
        org_slug = cfg.get("name", "")
        github_org = cfg.get("github_org", "")
        if org_slug and github_org:
            clone_memory_repo(org_slug, github_org)
    MEMORY_AVAILABLE = len(MEMORY_REPOS) > 0
    logger.info(f"Memory repos initialized: {list(MEMORY_REPOS.keys())}")


async def sync_memory_repos_background():
    """Background task: pull all memory repos every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        for org_slug, repo_dir in MEMORY_REPOS.items():
            try:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "pull", "--quiet"],
                    capture_output=True, timeout=30
                )
            except Exception as e:
                logger.warning(f"Memory sync failed for {org_slug}: {e}")


def search_memory(query: str, org_slug: str, max_results: int = 5) -> list[dict]:
    """Search memory repo markdown files for relevant content."""
    repo_dir = MEMORY_REPOS.get(org_slug)
    if not repo_dir or not repo_dir.exists():
        return []

    query_lower = query.lower()
    query_words = [w for w in query_lower.split() if len(w) > 2]
    results = []

    # Priority directories (searched first)
    priority_dirs = ["knowledge/decisions", "knowledge/findings", "knowledge/patterns", "handoffs", "people"]

    all_files = []
    for pdir in priority_dirs:
        full_dir = repo_dir / pdir
        if full_dir.exists():
            all_files.extend((f, True) for f in full_dir.rglob("*.md"))
    # Add remaining files with lower priority
    for f in repo_dir.rglob("*.md"):
        if not any(str(f).startswith(str(repo_dir / pd)) for pd in priority_dirs):
            all_files.append((f, False))

    for file_path, is_priority in all_files:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            content_lower = content.lower()

            # Score: count matching query words
            score = sum(1 for w in query_words if w in content_lower)
            if score == 0:
                continue

            # Boost priority directories
            if is_priority:
                score += 2

            # Extract title from first heading or filename
            title = file_path.stem
            for line in content.split("\n")[:5]:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break

            # Extract snippet around first match
            snippet = ""
            for word in query_words:
                idx = content_lower.find(word)
                if idx >= 0:
                    start = max(0, idx - 100)
                    end = min(len(content), idx + 200)
                    snippet = content[start:end].replace("\n", " ").strip()
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(content):
                        snippet = snippet + "..."
                    break

            rel_path = str(file_path.relative_to(repo_dir))
            results.append({"path": rel_path, "title": title, "snippet": snippet, "score": score})
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:max_results]


def load_org_identity(org_slug: str) -> str:
    """Load org identity text from memory repo (EGREGORE.md or soul.md)."""
    if org_slug in ORG_IDENTITIES:
        return ORG_IDENTITIES[org_slug]

    repo_dir = MEMORY_REPOS.get(org_slug)
    identity = ""

    if repo_dir:
        for fname in ["EGREGORE.md", "soul.md"]:
            fpath = repo_dir / fname
            if fpath.exists():
                try:
                    identity = fpath.read_text(encoding="utf-8", errors="ignore")[:2000]
                    break
                except Exception:
                    pass

    ORG_IDENTITIES[org_slug] = identity
    return identity


# =============================================================================
# PREDEFINED QUERIES
# =============================================================================

QUERIES = {
    "recent_activity": {
        "description": "Recent sessions/activity from the team (last 7 days)",
        "params": [],
        "cypher": """
            MATCH (s:Session)-[:BY]->(p:Person)
            WHERE date(s.date) >= date() - duration('P7D')
            RETURN s.date AS date, s.topic AS topic, p.name AS person, s.summary AS summary
            ORDER BY date(left(toString(s.date), 10)) DESC LIMIT 10
        """
    },
    "person_projects": {
        "description": "What projects a specific person works on",
        "params": ["name"],
        "cypher": """
            MATCH (p:Person {name: $name})-[w:WORKS_ON]->(proj:Project)
            RETURN proj.name AS project, proj.domain AS domain, w.role AS role
        """
    },
    "person_sessions": {
        "description": "Recent sessions by a specific person",
        "params": ["name"],
        "cypher": """
            MATCH (s:Session)-[:BY]->(p:Person {name: $name})
            RETURN s.date AS date, s.topic AS topic, s.summary AS summary
            ORDER BY date(left(toString(s.date), 10)) DESC LIMIT 5
        """
    },
    "quest_details": {
        "description": "Details about a specific quest",
        "params": ["quest_id"],
        "cypher": """
            MATCH (q:Quest {id: $quest_id})
            OPTIONAL MATCH (a:Artifact)-[:PART_OF]->(q)
            OPTIONAL MATCH (q)-[:STARTED_BY]->(p:Person)
            OPTIONAL MATCH (q)-[:RELATES_TO]->(proj:Project)
            RETURN q.title AS title, q.status AS status, q.question AS question,
                   p.name AS started_by,
                   collect(DISTINCT a.title) AS artifacts,
                   collect(DISTINCT proj.name) AS projects
        """
    },
    "active_quests": {
        "description": "All currently active quests",
        "params": [],
        "cypher": """
            MATCH (q:Quest {status: 'active'})
            OPTIONAL MATCH (q)-[:RELATES_TO]->(proj:Project)
            OPTIONAL MATCH (q)-[:STARTED_BY]->(p:Person)
            RETURN q.id AS id, q.title AS title,
                   collect(DISTINCT proj.name) AS projects,
                   p.name AS started_by
        """
    },
    "search_artifacts": {
        "description": "Search artifacts by keyword in title",
        "params": ["term"],
        "cypher": """
            MATCH (a:Artifact)
            WHERE toLower(a.title) CONTAINS toLower($term)
            OPTIONAL MATCH (a)-[:PART_OF]->(q:Quest)
            OPTIONAL MATCH (a)-[:CONTRIBUTED_BY]->(p:Person)
            RETURN a.title AS title, a.type AS type, a.created AS created,
                   p.name AS author, collect(DISTINCT q.id) AS quests
            LIMIT 10
        """
    },
    "all_people": {
        "description": "List all team members and their projects",
        "params": [],
        "cypher": """
            MATCH (p:Person)
            OPTIONAL MATCH (p)-[:WORKS_ON]->(proj:Project)
            RETURN p.name AS name, p.fullName AS fullName,
                   collect(DISTINCT proj.name) AS projects
        """
    },
    "project_details": {
        "description": "Details about a specific project",
        "params": ["name"],
        "cypher": """
            MATCH (proj:Project {name: $name})
            OPTIONAL MATCH (q:Quest {status: 'active'})-[:RELATES_TO]->(proj)
            OPTIONAL MATCH (p:Person)-[:WORKS_ON]->(proj)
            RETURN proj.name AS name, proj.domain AS domain,
                   proj.description AS description,
                   collect(DISTINCT q.id) AS quests,
                   collect(DISTINCT p.name) AS team
        """
    },
    "all_projects": {
        "description": "List all projects",
        "params": [],
        "cypher": """
            MATCH (proj:Project)
            OPTIONAL MATCH (p:Person)-[:WORKS_ON]->(proj)
            RETURN proj.name AS name, proj.domain AS domain,
                   collect(DISTINCT p.name) AS team
        """
    },
    "person_quests": {
        "description": "Quests started by a specific person",
        "params": ["name"],
        "cypher": """
            MATCH (q:Quest)-[:STARTED_BY]->(p:Person {name: $name})
            OPTIONAL MATCH (q)-[:RELATES_TO]->(proj:Project)
            WITH q, collect(DISTINCT proj.name) AS projects
            ORDER BY q.created DESC
            RETURN q.id AS id, q.title AS title, q.status AS status,
                   q.question AS question, projects
        """
    },
    "person_artifacts": {
        "description": "Artifacts contributed by a specific person",
        "params": ["name"],
        "cypher": """
            MATCH (a:Artifact)-[:CONTRIBUTED_BY]->(p:Person {name: $name})
            OPTIONAL MATCH (a)-[:PART_OF]->(q:Quest)
            WITH a, collect(DISTINCT q.id) AS quests
            ORDER BY a.created DESC
            RETURN a.title AS title, a.type AS type, a.created AS created, quests
            LIMIT 10
        """
    },
    "recent_artifacts": {
        "description": "Recently added artifacts",
        "params": [],
        "cypher": """
            MATCH (a:Artifact)
            OPTIONAL MATCH (a)-[:CONTRIBUTED_BY]->(p:Person)
            OPTIONAL MATCH (a)-[:PART_OF]->(q:Quest)
            WITH a, p, collect(DISTINCT q.id) AS quests
            ORDER BY a.created DESC LIMIT 10
            RETURN a.title AS title, a.type AS type, a.created AS created,
                   p.name AS author, quests
        """
    },
    "recent_quests": {
        "description": "Recently created quests",
        "params": [],
        "cypher": """
            MATCH (q:Quest)
            OPTIONAL MATCH (q)-[:STARTED_BY]->(p:Person)
            OPTIONAL MATCH (q)-[:RELATES_TO]->(proj:Project)
            WITH q, p, collect(DISTINCT proj.name) AS projects
            ORDER BY q.created DESC LIMIT 10
            RETURN q.id AS id, q.title AS title, q.status AS status,
                   p.name AS started_by, projects
        """
    },
    "activity_on_date": {
        "description": "All activity (sessions, artifacts) on a specific date. Use for 'what happened today/yesterday'",
        "params": ["date"],
        "cypher": """
            MATCH (s:Session)-[:BY]->(p:Person)
            WHERE date(s.date) = date($date)
            RETURN 'session' AS type, s.topic AS title, p.name AS person, s.summary AS summary, s.date AS date
            UNION
            MATCH (a:Artifact)-[:CONTRIBUTED_BY]->(p:Person)
            WHERE date(a.date) = date($date)
            RETURN 'artifact' AS type, a.name AS title, p.name AS person, NULL AS summary, a.date AS date
        """
    },
    "person_sessions_on_date": {
        "description": "Sessions by a specific person on a specific date. Use for 'what did X do today'",
        "params": ["name", "date"],
        "cypher": """
            MATCH (s:Session)-[:BY]->(p:Person {name: $name})
            WHERE date(s.date) = date($date)
            RETURN s.date AS date, s.topic AS topic, s.summary AS summary
            ORDER BY date(left(toString(s.date), 10)) DESC
        """
    },
    "handoffs_to_person": {
        "description": "Handoffs addressed to a specific person. Use for 'what was handed off to X'",
        "params": ["recipient"],
        "cypher": """
            MATCH (a:Artifact {type: 'handoff'})-[:FOR]->(recipient:Person {name: $recipient})
            OPTIONAL MATCH (a)-[:AUTHORED_BY]->(author:Person)
            RETURN a.title AS title, a.summary AS summary, a.date AS date,
                   author.name AS from_person
            ORDER BY a.date DESC
        """
    },
    "handoffs_from_person": {
        "description": "Handoffs written by a specific person. Use for 'what did X hand off'",
        "params": ["author"],
        "cypher": """
            MATCH (a:Artifact {type: 'handoff'})-[:AUTHORED_BY]->(author:Person {name: $author})
            OPTIONAL MATCH (a)-[:FOR]->(recipient:Person)
            RETURN a.title AS title, a.summary AS summary, a.date AS date,
                   recipient.name AS to_person
            ORDER BY a.date DESC
        """
    }
}


# =============================================================================
# LLM AGENT WITH TOOL USE
# =============================================================================

def build_tools_schema(graph_available: bool = True, memory_available: bool = False) -> list:
    """Build Anthropic tools schema based on available capabilities."""
    tools = []

    # Graph query tools (only if Neo4j is connected)
    if graph_available:
        for name, q in QUERIES.items():
            tool = {
                "name": f"query_{name}",
                "description": q["description"],
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
            for param in q.get("params", []):
                tool["input_schema"]["properties"][param] = {
                    "type": "string",
                    "description": f"The {param} (use lowercase for person names)"
                }
                tool["input_schema"]["required"].append(param)
            tools.append(tool)

        # Todo creation (requires graph)
        tools.append({
            "name": "create_todo",
            "description": "Create a todo item for the person asking. Use when they say 'add to my todo', 'remind me to', 'don't let me forget', or express intent to track a task.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The todo text (clean, actionable)"},
                    "priority": {"type": "integer", "description": "0=none, 1=low, 2=medium, 3=high. Detect from language: urgent/critical/ASAP=3, soon/important=2, eventually/maybe=1, default=0"}
                },
                "required": ["text"]
            }
        })

    # Memory search (only if memory repo is cloned)
    if memory_available:
        tools.append({
            "name": "search_memory",
            "description": "Search the knowledge base — handoffs, decisions, findings, patterns, people profiles. Use for institutional knowledge, past decisions, context about why something was done, or information not captured in the activity graph.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms — use specific keywords related to the topic"}
                },
                "required": ["query"]
            }
        })

    # Direct response tool (always available)
    tools.append({
        "name": "respond_directly",
        "description": "Respond directly without querying. Use for greetings, explaining what egregore is, general conversation, or when no data lookup is needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The response message"}
            },
            "required": ["message"]
        }
    })

    return tools


def build_system_prompt(
    org_config: dict,
    sender_name: str = None,
    conversation_context: str = "",
    today_str: str = None,
    team_names: list = None,
) -> str:
    """Build the system prompt for the agent, layered by context."""
    if not today_str:
        today_str = date.today().isoformat()

    org_slug = org_config.get("name", "default") if org_config else "default"

    # Layer 1: Identity
    org_identity = load_org_identity(org_slug)
    if org_identity:
        identity_block = f"""You are the voice of {org_slug}'s egregore — the shared intelligence that emerges from the team's collective work.

Here is the organization's identity:
{org_identity}

You speak as a colleague who has been in every session and knows the full context."""
    else:
        identity_block = f"""You are the voice of {org_slug}'s egregore — the shared intelligence that emerges from the team's collective work. You know the people, the projects, the decisions. You speak like a colleague who's been in every session.

An egregore is a shared intelligence layer for organizations — it gives teams persistent memory, async handoffs, and accumulated knowledge across sessions and people. The knowledge graph tracks sessions, quests, artifacts, projects, and people. The knowledge base stores decisions, findings, patterns, and handoffs as markdown."""

    # Layer 2: Capabilities
    capabilities = []
    if GRAPH_AVAILABLE:
        capabilities.append("- Activity graph: query sessions, quests, artifacts, projects, people, and handoffs")
        capabilities.append("- Todo creation: create personal todo items for team members")
    if MEMORY_AVAILABLE:
        capabilities.append("- Knowledge base: search decisions, findings, patterns, handoffs, and people profiles")
    cap_block = "\n".join(capabilities) if capabilities else "- Direct conversation only (no data sources connected)"

    # Layer 3: Sender identity
    sender_block = ""
    if sender_name:
        sender_block = f"""
SENDER: The person asking is "{sender_name}".
When they say "my", "I", "me", use name="{sender_name}" in queries."""

    # Layer 4: Team context
    team_line = ""
    if team_names:
        team_line = f"\nTEAM (use lowercase in queries): {', '.join(team_names)}"
    else:
        team_line = "\nTEAM: use query_all_people to discover team members"

    # Layer 5: Conversation history
    context_block = ""
    if conversation_context:
        context_block = f"\n\nCONVERSATION HISTORY:\n{conversation_context}\nUse this for follow-ups like 'which ones', 'tell me more', 'what about X', etc."

    return f"""{identity_block}

TODAY: {today_str}
{sender_block}
{team_line}

YOUR CAPABILITIES:
{cap_block}

QUERY ROUTING:
- "What is X working on?" / "What's X doing?" -> query_person_sessions (shows actual work, NOT query_person_projects)
- "What's happening?" -> query_recent_activity
- "What happened today?" -> query_activity_on_date(date="{today_str}")
- "What did X do today?" -> query_person_sessions_on_date(name="x", date="{today_str}")
- "Tell me about [quest]" -> query_quest_details or query_active_quests
- "What did X hand off?" -> query_handoffs_from_person
- Questions about decisions, patterns, or "why did we..." -> search_memory
- "Add to my todo" / "remind me to" -> create_todo
- Greetings, general questions, explanations -> respond_directly

BEHAVIORAL RULES:
- Conversational tone — like catching someone up over coffee
- No markdown formatting, no emojis
- Be specific — include names, dates, topics
- Keep responses concise — 2-3 short paragraphs max
- End with a casual follow-up when natural ("want details on any of those?")
- Skip intros and preamble — everyone knows each other
{context_block}"""


async def agent_decide(question: str, conversation_context: str = "", sender_name: str = None, org_config: dict = None) -> dict:
    """LLM agent with tool use decides what to do.

    Returns dict with:
        action: "respond", "query", "search_memory", or "create_todo"
        message: (if respond) the response text
        query: (if query) the query name
        params: (if query/search/todo) the parameters
        usage: {"input_tokens": int, "output_tokens": int}
        latency_ms: float
    """
    if not ANTHROPIC_API_KEY:
        return {"action": "respond", "message": "API not configured.", "usage": {}, "latency_ms": 0}

    today_str = date.today().isoformat()
    team_names = get_org_team_names(org_config) if GRAPH_AVAILABLE else []
    org_slug = org_config.get("name", "default") if org_config else "default"

    tools = build_tools_schema(
        graph_available=GRAPH_AVAILABLE,
        memory_available=org_slug in MEMORY_REPOS
    )

    system_prompt = build_system_prompt(
        org_config=org_config,
        sender_name=sender_name,
        conversation_context=conversation_context,
        today_str=today_str,
        team_names=team_names,
    )

    async with httpx.AsyncClient() as client:
        try:
            start_time = time.perf_counter()
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "tools": tools,
                    "messages": [{"role": "user", "content": question}]
                },
                timeout=30
            )
            latency_ms = (time.perf_counter() - start_time) * 1000
            resp.raise_for_status()
            data = resp.json()

            # Extract token usage
            usage = data.get("usage", {})
            usage_info = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0)
            }

            # Check for tool use
            for block in data.get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    logger.info(f"Agent chose: {tool_name} with {tool_input}")

                    if tool_name == "respond_directly":
                        return {"action": "respond", "message": tool_input.get("message", ""), "usage": usage_info, "latency_ms": latency_ms}

                    if tool_name == "search_memory":
                        return {"action": "search_memory", "params": tool_input, "usage": usage_info, "latency_ms": latency_ms}

                    if tool_name == "create_todo":
                        return {"action": "create_todo", "params": tool_input, "usage": usage_info, "latency_ms": latency_ms}

                    if tool_name.startswith("query_"):
                        query_name = tool_name[6:]
                        return {"action": "query", "query": query_name, "params": tool_input, "usage": usage_info, "latency_ms": latency_ms}

            # Fallback to text response
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return {"action": "respond", "message": block.get("text", ""), "usage": usage_info, "latency_ms": latency_ms}

            return {"action": "respond", "message": "I'm not sure how to help with that.", "usage": usage_info, "latency_ms": latency_ms}

        except Exception as e:
            logger.error(f"Agent failed: {e}")
            return {"action": "respond", "message": "Something went wrong. Try asking differently?", "usage": {}, "latency_ms": 0}


async def format_response(question: str, query_name: str, results: list, params: dict = None, org_config: dict = None) -> tuple:
    """Use LLM to format query results as conversational text.

    Returns tuple of (response_text, usage_dict, latency_ms)
    """
    if not ANTHROPIC_API_KEY:
        return f"Found {len(results)} results.", {}, 0

    if not results:
        return "No results found.", {}, 0

    org_name = org_config.get("name", "the team") if org_config else "the team"
    system_prompt = f"""You are Egregore, shared memory for {org_name}. Internal tool - everyone knows each other.

FORMAT RULES:

FOR EXPLICIT LIST QUERIES ("what quests are active", "who's on the team", "list projects"):
- Use clean list format: one item per line
- Include key info per item

FOR EVERYTHING ELSE (activity, "what's X doing", updates):
- Write naturally like you're catching someone up over coffee
- "Cem's been deep in the evaluation stuff - surveyed 80+ datasets and landed on LLMs4OL Task B. Also wrapped up the Egregore Spec and handed it to Oz."
- NOT log format: "Cem - 4 sessions: Item 1, Item 2..."
- Weave the information into sentences, mention what's interesting
- 2-3 short paragraphs max

TONE:
- Conversational, not structured
- Skip intros - everyone knows each other
- Include specifics (dates, names) but naturally
- End with casual follow-up

BAD (log-like): "Cem - 4 sessions this week: Egregore Spec V2 (Jan 28), Evaluation benchmarks (Jan 26)..."
GOOD (natural): "Cem's been heads-down on evaluation benchmarks - surveyed 80+ datasets and locked in LLMs4OL Task B. The big handoff was the Egregore Spec V2 going to Oz, ready for the blog launch."

NO markdown, NO emojis."""

    async with httpx.AsyncClient() as client:
        try:
            start_time = time.perf_counter()
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "messages": [{
                        "role": "user",
                        "content": f"Question: {question}\nQuery type: {query_name}\nData: {json.dumps(results, default=str)}"
                    }]
                },
                timeout=30
            )
            latency_ms = (time.perf_counter() - start_time) * 1000
            resp.raise_for_status()
            data = resp.json()

            usage = data.get("usage", {})
            usage_info = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0)
            }

            return data["content"][0]["text"], usage_info, latency_ms
        except Exception as e:
            logger.error(f"LLM response formatter failed: {e}")
            return f"Found {len(results)} results for {query_name}.", {}, 0


async def generate_no_results_response(question: str, query_name: str, params: dict, org_config: dict = None) -> str:
    """Generate a helpful response when no results are found."""
    if not ANTHROPIC_API_KEY:
        return "Nothing in the graph for that yet. Try a different angle?"

    org_name = org_config.get("name", "the team") if org_config else "the team"
    search_context = f"Query: {query_name}, Params: {params}"

    system_prompt = f"""You are the voice of {org_name}'s egregore. Talking to INTERNAL team members.
A search returned no results. Keep it brief and casual:

1. Quick acknowledgment (not apologetic)
2. Suggest what they could try instead
3. One line max

TONE: Like a colleague saying "nothing on that yet, try X"
NO markdown, NO emojis, NO formal language."""

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": 800,
                    "system": system_prompt,
                    "messages": [{
                        "role": "user",
                        "content": f"User asked: {question}\n{search_context}\n\nNo results were found. Provide a helpful response."
                    }]
                },
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as e:
            logger.error(f"No results response failed: {e}")
            return "Nothing in the graph for that yet. Try asking about team activity, people, or quests."


# =============================================================================
# TODO CREATION
# =============================================================================

async def handle_create_todo(text: str, priority: int, sender_name: str, org_config: dict) -> str:
    """Create a Todo node in Neo4j. Returns confirmation message."""
    if not sender_name:
        return "I don't know who you are — can't create a todo without knowing the owner."

    today = date.today().isoformat()

    # Get next sequence number
    count_results = run_org_query(
        "MATCH (t:Todo)-[:BY]->(p:Person {name: $name}) WHERE t.id STARTS WITH $prefix RETURN count(t) AS count",
        {"name": sender_name, "prefix": today},
        org_config,
    )
    seq = (count_results[0]["count"] if count_results else 0) + 1
    todo_id = f"{today}-{sender_name}-{seq:03d}"

    # Extract topic words for quest matching
    stop_words = {"the", "a", "an", "to", "for", "and", "or", "but", "in", "on", "at", "of", "is", "it", "my", "me", "i"}
    topic_words = [w.lower() for w in re.split(r'\W+', text) if len(w) > 2 and w.lower() not in stop_words]

    # Check for quest match
    quest_id = None
    if topic_words:
        active_quests = run_org_query(
            "MATCH (q:Quest {status: 'active'}) RETURN q.id AS id, q.title AS title",
            {},
            org_config,
        )
        for quest in active_quests:
            qid = quest.get("id", "")
            qtitle = quest.get("title", "")
            quest_words = set(re.split(r'[\W_-]+', f"{qid} {qtitle}".lower()))
            overlap = sum(1 for w in topic_words if w in quest_words)
            if overlap >= 2 or qid in text.lower():
                quest_id = qid
                break

    # Create the todo node
    run_org_query(
        """MATCH (p:Person {name: $name})
        CREATE (t:Todo {id: $id, text: $text, status: 'open', created: datetime(),
                        completed: null, priority: $priority, topics: $topics, source: 'telegram'})
        CREATE (t)-[:BY]->(p)
        RETURN t.id AS id""",
        {"name": sender_name, "id": todo_id, "text": text, "priority": priority, "topics": topic_words},
        org_config,
    )

    # Link to quest if matched
    if quest_id:
        run_org_query(
            "MATCH (t:Todo {id: $tid}) MATCH (q:Quest {id: $qid}) CREATE (t)-[:PART_OF]->(q)",
            {"tid": todo_id, "qid": quest_id},
            org_config,
        )

    # Count open todos
    count = run_org_query(
        "MATCH (t:Todo)-[:BY]->(p:Person {name: $name}) WHERE t.status IN ['open', 'blocked', 'deferred'] RETURN count(t) AS count",
        {"name": sender_name},
        org_config,
    )
    open_count = count[0]["count"] if count else "?"

    quest_note = f" (linked to {quest_id})" if quest_id else ""
    return f"Added: {text}{quest_note}. {open_count} open todos."


# =============================================================================
# MEMORY SEARCH FORMATTING
# =============================================================================

async def format_memory_results(question: str, results: list[dict], org_config: dict = None) -> tuple:
    """Format memory search results as conversational text via LLM.

    Returns tuple of (response_text, usage_dict, latency_ms)
    """
    if not results:
        return "Nothing in the knowledge base for that. Try different keywords?", {}, 0

    if not ANTHROPIC_API_KEY:
        lines = [f"- {r['title']}: {r['snippet'][:100]}" for r in results]
        return "Found in knowledge base:\n" + "\n".join(lines), {}, 0

    org_name = org_config.get("name", "the team") if org_config else "the team"
    results_text = "\n\n".join(
        f"### {r['title']} ({r['path']})\n{r['snippet']}" for r in results
    )

    system_prompt = f"""You are the voice of {org_name}'s egregore. Synthesize the knowledge base results into a conversational answer.

RULES:
- Answer the question using the search results as context
- Weave information naturally — don't just list what you found
- Mention which document/decision the info comes from when relevant
- If results are tangential, say so and suggest what else to try
- Conversational tone, no markdown, no emojis
- 2-3 short paragraphs max"""

    async with httpx.AsyncClient() as client:
        try:
            start_time = time.perf_counter()
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "messages": [{
                        "role": "user",
                        "content": f"Question: {question}\n\nKnowledge base results:\n{results_text}"
                    }]
                },
                timeout=30
            )
            latency_ms = (time.perf_counter() - start_time) * 1000
            resp.raise_for_status()
            data = resp.json()

            usage = data.get("usage", {})
            usage_info = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0)
            }

            return data["content"][0]["text"], usage_info, latency_ms
        except Exception as e:
            logger.error(f"Memory format failed: {e}")
            lines = [f"- {r['title']}: {r['snippet'][:100]}" for r in results]
            return "Found in knowledge base:\n" + "\n".join(lines), {}, 0


# =============================================================================
# CONVERSATION CONTEXT
# =============================================================================

MAX_HISTORY = 8

def get_conversation_context(context) -> str:
    """Get recent conversation history for follow-ups."""
    history = context.chat_data.get("history", [])
    if not history:
        return ""
    
    lines = []
    for entry in history[-3:]:  # Last 3 exchanges
        lines.append(f"Q: {entry['question']}")
        lines.append(f"A: {entry['summary']}")
    return "\n".join(lines)


def store_in_context(context, question: str, query_name: str, result_summary: str):
    """Store exchange in conversation context."""
    if "history" not in context.chat_data:
        context.chat_data["history"] = []
    
    context.chat_data["history"].append({
        "question": question,
        "query": query_name,
        "summary": result_summary
    })
    
    # Trim old entries
    if len(context.chat_data["history"]) > MAX_HISTORY:
        context.chat_data["history"] = context.chat_data["history"][-MAX_HISTORY:]


# =============================================================================
# MAIN HANDLER
# =============================================================================

def is_allowed(update: Update) -> bool:
    """Check if chat/user is allowed."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    return chat_id in ALLOWED_CHAT_IDS or user_id in ALLOWED_CHAT_IDS or chat_id in ORG_CONFIG


def resolve_orgs_for_dm(telegram_id: int) -> list[dict]:
    """For DMs, find all orgs a user belongs to by checking Person nodes across all orgs."""
    matches = []
    for cid, cfg in ORG_CONFIG.items():
        name = lookup_person_by_telegram_id(telegram_id, org_config=cfg)
        if name:
            matches.append(cfg)
    return matches


async def handle_question(update: Update, context, question: str, org_config: dict = None) -> None:
    """Main question handler - agent decides what to do."""

    # Get org config — passed in for DMs, otherwise from chat_id
    chat_id = update.effective_chat.id
    if not org_config:
        org_config = ORG_CONFIG.get(chat_id)
    if not org_config:
        await update.message.reply_text("This group isn't connected to an Egregore org yet. Ask your admin to add the bot through the setup flow.")
        return

    org_name = org_config.get("name", "default")
    logger.info(f"handle_question: chat_id={chat_id}, org={org_name}")

    # Track user info for analytics
    user_id = None
    sender_name = None
    if update.effective_user:
        user_id = update.effective_user.id
        first_name = update.effective_user.first_name
        username = update.effective_user.username

        # Try lookup first, then auto-register if not found
        if GRAPH_AVAILABLE:
            sender_name = lookup_person_by_telegram_id(user_id, org_config=org_config)
            if not sender_name:
                sender_name = auto_register_telegram_id(user_id, first_name, username=username, org_config=org_config)
            elif username:
                run_query(
                    "MATCH (tu:TelegramUser {telegramId: $tid}) SET tu.username = $username",
                    {"tid": user_id, "username": username},
                )

        if sender_name:
            logger.info(f"Identified sender: {sender_name} (Telegram ID: {user_id})")

    # Get conversation context for follow-ups
    conv_context = get_conversation_context(context)

    # Agent decides (tool use)
    decision = await agent_decide(question, conv_context, sender_name, org_config)

    action = decision.get("action")
    decision_usage = decision.get("usage", {})
    decision_latency = decision.get("latency_ms", 0)

    if action == "respond":
        response = decision.get("message", "")
        await update.message.reply_text(response)
        store_in_context(context, question, "direct", response[:100])
        log_query_event(
            query_type="direct",
            tokens_in=decision_usage.get("input_tokens", 0),
            tokens_out=decision_usage.get("output_tokens", 0),
            latency_ms=decision_latency,
            results_count=0,
            success=True,
            user_id=user_id,
            user_name=sender_name,
            question=question,
            decision_tokens_in=decision_usage.get("input_tokens", 0),
            decision_tokens_out=decision_usage.get("output_tokens", 0),
            decision_latency_ms=decision_latency,
        )
        return

    if action == "create_todo":
        params = decision.get("params", {})
        response = await handle_create_todo(
            text=params.get("text", ""),
            priority=params.get("priority", 0),
            sender_name=sender_name,
            org_config=org_config,
        )
        await update.message.reply_text(response)
        store_in_context(context, question, "create_todo", response[:100])
        log_query_event(
            query_type="create_todo",
            tokens_in=decision_usage.get("input_tokens", 0),
            tokens_out=decision_usage.get("output_tokens", 0),
            latency_ms=decision_latency,
            results_count=1,
            success=True,
            user_id=user_id,
            user_name=sender_name,
            question=question,
            decision_tokens_in=decision_usage.get("input_tokens", 0),
            decision_tokens_out=decision_usage.get("output_tokens", 0),
            decision_latency_ms=decision_latency,
        )
        return

    if action == "search_memory":
        params = decision.get("params", {})
        query_text = params.get("query", question)
        results = search_memory(query_text, org_name)

        response, format_usage, format_latency = await format_memory_results(question, results, org_config)
        await update.message.reply_text(response)
        store_in_context(context, question, "search_memory", f"{len(results)} results")

        total_tokens_in = decision_usage.get("input_tokens", 0) + format_usage.get("input_tokens", 0)
        total_tokens_out = decision_usage.get("output_tokens", 0) + format_usage.get("output_tokens", 0)
        log_query_event(
            query_type="search_memory",
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms=decision_latency + format_latency,
            results_count=len(results),
            success=True,
            user_id=user_id,
            user_name=sender_name,
            question=question,
            decision_tokens_in=decision_usage.get("input_tokens", 0),
            decision_tokens_out=decision_usage.get("output_tokens", 0),
            decision_latency_ms=decision_latency,
            format_tokens_in=format_usage.get("input_tokens", 0),
            format_tokens_out=format_usage.get("output_tokens", 0),
            format_latency_ms=format_latency,
        )
        return

    if action == "query":
        query_name = decision.get("query")
        params = decision.get("params", {})

        if query_name not in QUERIES:
            await update.message.reply_text("I couldn't find that information.")
            return

        cypher = QUERIES[query_name]["cypher"]
        logger.info(f"Running query: {query_name} with params: {params} (org: {org_name})")

        neo4j_start = time.perf_counter()
        results = run_org_query(cypher, params, org_config)
        neo4j_latency = (time.perf_counter() - neo4j_start) * 1000

        if not results:
            helpful_msg = await generate_no_results_response(question, query_name, params, org_config)
            await update.message.reply_text(helpful_msg)
            log_query_event(
                query_type=query_name,
                tokens_in=decision_usage.get("input_tokens", 0),
                tokens_out=decision_usage.get("output_tokens", 0),
                latency_ms=decision_latency + neo4j_latency,
                results_count=0,
                success=True,
                user_id=user_id,
                user_name=sender_name,
                question=question,
                decision_tokens_in=decision_usage.get("input_tokens", 0),
                decision_tokens_out=decision_usage.get("output_tokens", 0),
                decision_latency_ms=decision_latency,
                neo4j_latency_ms=neo4j_latency,
            )
            return

        response, format_usage, format_latency = await format_response(question, query_name, results, params, org_config)
        await update.message.reply_text(response)

        summary = f"{query_name}: {len(results)} results"
        store_in_context(context, question, query_name, summary)

        total_tokens_in = decision_usage.get("input_tokens", 0) + format_usage.get("input_tokens", 0)
        total_tokens_out = decision_usage.get("output_tokens", 0) + format_usage.get("output_tokens", 0)
        total_latency = decision_latency + neo4j_latency + format_latency
        log_query_event(
            query_type=query_name,
            tokens_in=total_tokens_in,
            tokens_out=total_tokens_out,
            latency_ms=total_latency,
            results_count=len(results),
            success=True,
            user_id=user_id,
            user_name=sender_name,
            question=question,
            decision_tokens_in=decision_usage.get("input_tokens", 0),
            decision_tokens_out=decision_usage.get("output_tokens", 0),
            decision_latency_ms=decision_latency,
            format_tokens_in=format_usage.get("input_tokens", 0),
            format_tokens_out=format_usage.get("output_tokens", 0),
            format_latency_ms=format_latency,
            neo4j_latency_ms=neo4j_latency,
        )
        return

    # Fallback
    await update.message.reply_text("I'm not sure how to help with that.")


# =============================================================================
# TELEGRAM HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command.

    In groups: check for startgroup deep link payload (org_SLUG) to register the group.
    In private: start onboarding flow.
    """
    user = update.effective_user
    chat_type = update.effective_chat.type

    # Handle startgroup deep link in group chats: /start org_SLUG
    if chat_type in ("group", "supergroup") and context.args:
        payload = context.args[0]
        if payload.startswith("org_"):
            slug = payload[4:]  # Strip "org_" prefix
            chat_id = update.effective_chat.id
            logger.info(f"Group registration: slug={slug}, chat_id={chat_id}")

            group_title = update.effective_chat.title
            group_username = update.effective_chat.username
            result = await register_group(slug, chat_id, group_title=group_title, group_username=group_username)
            if result:
                org_name = result.get("org_name", slug)
                # Add to local config (use module-level constants that derive URI from HOST)
                shared_uri = EGREGORE_NEO4J_URI
                shared_user = EGREGORE_NEO4J_USER
                shared_password = EGREGORE_NEO4J_PASSWORD
                ORG_CONFIG[chat_id] = {
                    "name": slug,
                    "neo4j_uri": shared_uri,
                    "neo4j_user": shared_user,
                    "neo4j_password": shared_password,
                }
                if chat_id not in ALLOWED_CHAT_IDS:
                    ALLOWED_CHAT_IDS.append(chat_id)

                await update.message.reply_text(
                    f"Connected to {org_name}! Notifications will appear here.\n\n"
                    "Ask me anything — I can search the knowledge graph, show activity, and more."
                )
            else:
                await update.message.reply_text(
                    "Couldn't connect this group. Make sure the org exists in Egregore."
                )
            return

    # In private chat, point to website
    if chat_type == "private":
        site_url = os.environ.get("EGREGORE_SITE_URL", "https://egregore-core.netlify.app")
        await update.message.reply_text(
            "Welcome to Egregore!\n\n"
            f"Get set up here: {site_url}/setup\n\n"
            "Already set up? Ask me anything about your org's work."
        )
        return

    if not is_allowed(update):
        await update.message.reply_text("This bot is private to Egregore.")
        return

    await update.message.reply_text(
        "I'm Egregore. Ask me anything about our work:\n\n"
        "- What's happening?\n"
        "- What is Oz working on?\n"
        "- What quests are active?\n"
        "- Tell me about the FAMP proposal\n"
        "- Who's on the team?\n\n"
        "Or ask how to use Egregore (commands, adding artifacts, etc.)"
    )


async def activity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /activity command."""
    if not is_allowed(update):
        return
    await handle_question(update, context, "What's been happening recently?")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /debug command - show deployment info."""
    if not is_allowed(update):
        return

    from pathlib import Path
    import os

    cwd = os.getcwd()
    file_loc = Path(__file__).parent

    # List files in likely locations
    locations = {
        "cwd": Path(cwd),
        "__file__.parent": file_loc,
        "/app": Path("/app"),
        "/app/telegram-bot": Path("/app/telegram-bot"),
    }

    lines = [f"CWD: {cwd}", f"__file__: {__file__}", ""]

    for name, path in locations.items():
        if path.exists():
            files = list(path.glob("*.zip")) + list(path.glob("*.py"))[:3]
            lines.append(f"{name}: {[f.name for f in files]}")
        else:
            lines.append(f"{name}: (not found)")

    await update.message.reply_text("\n".join(lines))


async def isolation_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /isolation command — run live org isolation check."""
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    org_config = ORG_CONFIG.get(chat_id)

    if not org_config:
        await update.message.reply_text(
            f"No org config for chat {chat_id}.\n"
            f"Known chats: {list(ORG_CONFIG.keys())}"
        )
        return

    org_name = org_config.get("name", "?")
    neo4j_uri = org_config.get("neo4j_uri", "")
    lines = [f"Isolation test for org: {org_name}"]
    lines.append(f"DB: {'shared' if neo4j_uri and 'c02b' not in neo4j_uri else 'CL-private' if neo4j_uri else 'NONE'}")
    lines.append(f"Driver: {'OK' if get_org_driver(org_config) else 'FAILED'}\n")

    # 1. Check team scoping — only this org's people
    people = run_org_query("MATCH (p:Person) RETURN p.name AS name, p.org AS org", {}, org_config)
    lines.append(f"People found: {len(people)}")
    if people:
        names = [p.get("name") or "(unnamed)" for p in people[:8]]
        lines.append(f"  Names: {', '.join(names)}")
    wrong_org = [p for p in people if p.get("org") and p["org"] != org_name]
    if wrong_org:
        lines.append(f"FAIL: {len(wrong_org)} person(s) from other orgs leaked!")
        for p in wrong_org[:5]:
            lines.append(f"  - {p.get('name')} (org: {p.get('org')})")
    else:
        lines.append("PASS: All people belong to this org")

    # 2. Check sessions scoping
    sessions = run_org_query(
        "MATCH (s:Session) RETURN s.topic AS topic, s.org AS org LIMIT 20", {}, org_config
    )
    lines.append(f"\nSessions found: {len(sessions)}")
    wrong_sessions = [s for s in sessions if s.get("org") and s["org"] != org_name]
    if wrong_sessions:
        lines.append(f"FAIL: {len(wrong_sessions)} session(s) from other orgs!")
        for s in wrong_sessions[:3]:
            lines.append(f"  - {s.get('topic')} (org: {s.get('org')})")
    else:
        lines.append("PASS: All sessions belong to this org")

    # 3. Check artifacts scoping
    artifacts = run_org_query(
        "MATCH (a:Artifact) RETURN a.title AS title, a.org AS org LIMIT 20", {}, org_config
    )
    lines.append(f"\nArtifacts found: {len(artifacts)}")
    wrong_artifacts = [a for a in artifacts if a.get("org") and a["org"] != org_name]
    if wrong_artifacts:
        lines.append(f"FAIL: {len(wrong_artifacts)} artifact(s) from other orgs!")
    else:
        lines.append("PASS: All artifacts belong to this org")

    # 4. Fake org test — should return nothing
    fake_config = dict(org_config)
    fake_config["name"] = "__isolation_test_fake__"
    fake_people = run_org_query("MATCH (p:Person) RETURN p.name", {}, fake_config)
    if fake_people:
        lines.append(f"\nFAIL: Fake org returned {len(fake_people)} person(s)!")
    else:
        lines.append("\nPASS: Fake org returns 0 results")

    await update.message.reply_text("\n".join(lines))


async def onboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /onboard command — redirect to website."""
    site_url = os.environ.get("EGREGORE_SITE_URL", "https://egregore-core.netlify.app")
    await update.message.reply_text(f"Get set up here: {site_url}/setup")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any text message."""
    if not update.message or not update.message.text:
        return

    chat_type = update.effective_chat.type
    text = update.message.text.strip()

    # DM mode — resolve org from sender's identity
    if chat_type == "private":
        if await handle_onboarding_dm(update, context):
            return

        if update.effective_user and GRAPH_AVAILABLE:
            # Check if user already selected an org this session
            dm_org = context.user_data.get("dm_org") if context.user_data else None
            if dm_org:
                await handle_question(update, context, text, org_config=dm_org)
                return

            # /switch command to change org
            if text.lower().startswith("/switch"):
                if context.user_data:
                    context.user_data.pop("dm_org", None)
                orgs = resolve_orgs_for_dm(update.effective_user.id)
                if len(orgs) > 1:
                    names = "\n".join(f"  {i+1}. {cfg['name']}" for i, cfg in enumerate(orgs))
                    await update.message.reply_text(f"Which egregore?\n{names}\n\nReply with the number.")
                    context.user_data["dm_org_choices"] = orgs
                    return
                elif orgs:
                    context.user_data["dm_org"] = orgs[0]
                    await update.message.reply_text(f"Switched to {orgs[0]['name']}.")
                    return

            # Check if user is picking from a list
            choices = context.user_data.get("dm_org_choices") if context.user_data else None
            if choices and text.strip().isdigit():
                idx = int(text.strip()) - 1
                if 0 <= idx < len(choices):
                    context.user_data["dm_org"] = choices[idx]
                    context.user_data.pop("dm_org_choices", None)
                    await update.message.reply_text(f"Talking to {choices[idx]['name']}. Send /switch to change.")
                    return

            # Find all orgs user belongs to
            orgs = resolve_orgs_for_dm(update.effective_user.id)
            if len(orgs) > 1:
                names = "\n".join(f"  {i+1}. {cfg['name']}" for i, cfg in enumerate(orgs))
                await update.message.reply_text(f"You're in {len(orgs)} egregores:\n{names}\n\nReply with the number.")
                context.user_data["dm_org_choices"] = orgs
                return
            elif orgs:
                context.user_data["dm_org"] = orgs[0]
                await handle_question(update, context, text, org_config=orgs[0])
                return

        await update.message.reply_text(
            "I don't recognize you yet. Ask your admin to connect your Telegram account to your egregore profile."
        )
        return

    # Group mode — standard flow
    if not is_allowed(update):
        return

    if chat_type in ["group", "supergroup"]:
        bot_username = context.bot.username
        if f"@{bot_username}" not in text:
            if not (update.message.reply_to_message and
                    update.message.reply_to_message.from_user.id == context.bot.id):
                return
        text = text.replace(f"@{bot_username}", "").strip()

    if not text:
        return

    await handle_question(update, context, text)


# =============================================================================
# NOTIFICATIONS
# =============================================================================

def get_telegram_id(name: str, org_config: dict = None) -> Optional[int]:
    """Look up Telegram ID by person name from Neo4j.

    Matches against name, fullName, or aliases (case-insensitive).
    Uses org-scoped query to prevent cross-org person resolution.
    """
    name_lower = name.lower().strip()
    _run = (lambda q, p: run_org_query(q, p, org_config)) if org_config else run_query

    # Try exact match on name first (fastest)
    results = _run(
        "MATCH (p:Person {name: $name}) WHERE p.telegramId IS NOT NULL RETURN p.telegramId AS telegramId",
        {"name": name_lower}
    )
    if results and results[0].get("telegramId"):
        return int(results[0]["telegramId"])

    # Try fuzzy match on fullName or aliases
    results = _run(
        """MATCH (p:Person)
           WHERE p.telegramId IS NOT NULL AND (
               toLower(p.fullName) CONTAINS $name
               OR $name IN [x IN coalesce(p.aliases, []) | toLower(x)]
           )
           RETURN p.telegramId AS telegramId
           LIMIT 1""",
        {"name": name_lower}
    )
    if results and results[0].get("telegramId"):
        return int(results[0]["telegramId"])

    return None


async def send_notification(bot, recipient: str, message: str, notification_type: str = "mention", org_config: dict = None) -> bool:
    """Send a notification to a team member."""
    telegram_id = get_telegram_id(recipient, org_config=org_config)
    if not telegram_id:
        logger.warning(f"No Telegram ID found for {recipient}")
        return False

    try:
        await bot.send_message(chat_id=telegram_id, text=message)
        logger.info(f"Notification sent to {recipient} ({telegram_id})")
        return True
    except Exception as e:
        logger.error(f"Failed to send notification to {recipient}: {e}")
        return False


# Global reference to bot for HTTP handler
telegram_bot = None


async def handle_notify_request(request: Request) -> JSONResponse:
    """HTTP endpoint for sending notifications.

    POST /notify
    {
        "recipient": "oz",           # Person name (lowercase)
        "message": "...",            # Notification message
        "type": "handoff|quest|mention"  # Optional
    }
    """
    global telegram_bot

    if not telegram_bot:
        return JSONResponse({"error": "Bot not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    recipient = data.get("recipient")
    message = data.get("message")
    notification_type = data.get("type", "mention")
    org_slug = data.get("org")  # Optional: scope person lookup to an org

    if not recipient or not message:
        return JSONResponse({"error": "Missing recipient or message"}, status_code=400)

    # Resolve org config for scoped person lookup
    notify_org_config = None
    if org_slug:
        for cid, cfg in ORG_CONFIG.items():
            if cfg.get("name") == org_slug:
                notify_org_config = cfg
                break

    success = await send_notification(telegram_bot, recipient, message, notification_type, org_config=notify_org_config)

    if success:
        return JSONResponse({"status": "sent", "recipient": recipient})
    else:
        return JSONResponse({"error": f"Could not notify {recipient}"}, status_code=404)


# =============================================================================
# NEW MEMBER ONBOARDING
# =============================================================================

async def handle_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle members joining or leaving a group — track membership + welcome."""
    if not update.chat_member:
        return

    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status
    member = update.chat_member.new_chat_member.user
    chat_id = update.effective_chat.id

    if chat_id not in ORG_CONFIG:
        return

    org_config = ORG_CONFIG[chat_id]
    org_slug = org_config.get("name", "default")

    # Detect join
    if new_status in ["member", "administrator"] and old_status not in ["member", "administrator"]:
        logger.info(f"Member joined: {member.first_name} ({member.id}) → {org_slug}")
        track_telegram_membership(
            telegram_id=member.id,
            username=member.username or "",
            first_name=member.first_name or "",
            org_slug=org_slug,
            action="join",
        )
        site_url = os.environ.get("EGREGORE_SITE_URL", "https://egregore-core.netlify.app")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Welcome {member.first_name}! Get set up here: {site_url}/setup",
        )

    # Detect leave
    elif old_status in ["member", "administrator"] and new_status in ["left", "kicked"]:
        logger.info(f"Member left: {member.first_name} ({member.id}) from {org_slug}")
        track_telegram_membership(
            telegram_id=member.id,
            username=member.username or "",
            first_name=member.first_name or "",
            org_slug=org_slug,
            action="leave",
        )


async def handle_onboarding_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Legacy DM handler — no longer used for onboarding. Returns False."""
    return False


# =============================================================================
# SPIRIT ADAPTER ENDPOINTS
# =============================================================================

async def handle_spirit_init(request: Request) -> JSONResponse:
    """Initialize a Spirit node in the bot's database.

    POST /spirit/init
    Headers: X-Admin-Secret: <SPIRIT_ADMIN_SECRET>
    {
        "spirit_id": "spirit-openclaw-cem-alter",
        "name": "Alter",
        "vessel": "cem",
        "platform": "openclaw",
        "trust_level": "elevated"
    }
    """
    # Require admin secret
    if not SPIRIT_ADMIN_SECRET:
        return JSONResponse({"error": "Spirit init not configured"}, status_code=503)

    admin_secret = request.headers.get("X-Admin-Secret", "")
    if not secrets.compare_digest(admin_secret, SPIRIT_ADMIN_SECRET):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    spirit_id = data.get("spirit_id")
    name = data.get("name")
    vessel = data.get("vessel")
    platform = data.get("platform", "unknown")
    trust_level = data.get("trust_level", "standard")

    if not all([spirit_id, name, vessel]):
        return JSONResponse({"error": "Missing required fields"}, status_code=400)

    # Create Spirit node with registration token
    reg_token = f"reg-{uuid.uuid4()}"

    result = run_query("""
        MATCH (p:Person {name: $vessel})
        CREATE (s:Spirit {
            id: $spiritId,
            name: $name,
            platform: $platform,
            status: "pending",
            trustLevel: $trustLevel,
            registrationToken: $regToken,
            tokenExpiresAt: datetime() + duration('PT2H'),
            created: datetime()
        })
        CREATE (s)-[:INVOKED_BY]->(p)
        RETURN s.id AS id, s.registrationToken AS token, s.tokenExpiresAt AS expires
    """, {
        "spiritId": spirit_id,
        "name": name,
        "vessel": vessel,
        "platform": platform,
        "trustLevel": trust_level,
        "regToken": reg_token
    })

    if not result:
        return JSONResponse({"error": "Failed to create Spirit (vessel not found?)"}, status_code=404)

    return JSONResponse({
        "status": "created",
        "spirit_id": result[0]["id"],
        "registration_token": result[0]["token"],
        "expires": str(result[0]["expires"])
    })


async def handle_spirit_activate(request: Request) -> JSONResponse:
    """Activate a pending external Spirit.

    POST /spirit/activate
    {
        "registration_token": "reg-...",
        "platform": {"name": "openclaw", "version": "2026.2.2"},
        "endpoint": "http://localhost:8765",
        "capabilities": ["shell", "web", "fs"]
    }

    Returns API key (one-time) on success.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    registration_token = data.get("registration_token")
    platform_info = data.get("platform", {})
    endpoint = data.get("endpoint")
    capabilities = data.get("capabilities", [])

    if not registration_token:
        return JSONResponse({"error": "Missing registration_token"}, status_code=400)

    logger.info(f"Spirit activation attempt with token: {registration_token[:20]}...")

    # Find pending Spirit with this token (not expired)
    result = run_query("""
        MATCH (s:Spirit {registrationToken: $token, status: "pending"})
        WHERE s.tokenExpiresAt > datetime()
        RETURN s.id AS id, s.name AS name, s.trustLevel AS trustLevel
    """, {"token": registration_token})

    logger.info(f"Spirit query result: {result}")

    if not result:
        logger.info(f"Spirit activation failed: no matching pending Spirit for token {registration_token[:20]}...")
        return JSONResponse({"error": "Invalid or expired token"}, status_code=404)

    spirit = result[0]
    spirit_id = spirit["id"]
    spirit_name = spirit["name"]

    # Generate API key: sk_egregore_[short_id]_[random]
    short_id = spirit_id.replace("spirit-", "")[:15].replace("-", "_")
    api_key = f"sk_egregore_{short_id}_{secrets.token_hex(12)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Activate the Spirit
    run_query("""
        MATCH (s:Spirit {registrationToken: $token})
        SET s.status = "manifest",
            s.apiKeyHash = $apiKeyHash,
            s.endpoint = $endpoint,
            s.platformVersion = $version,
            s.capabilities = $capabilities,
            s.activatedAt = datetime(),
            s.lastHeartbeat = datetime()
        REMOVE s.registrationToken, s.tokenExpiresAt
    """, {
        "token": registration_token,
        "apiKeyHash": api_key_hash,
        "endpoint": endpoint,
        "version": platform_info.get("version", "unknown"),
        "capabilities": capabilities
    })

    # Log the activation
    log_event(
        component="spirit",
        operation="activate",
        success=True,
        metadata={
            "spirit_id": spirit_id,
            "spirit_name": spirit_name,
            "platform": platform_info.get("name", "unknown"),
            "platform_version": platform_info.get("version", "unknown"),
            "capabilities": capabilities
        }
    )

    logger.info(f"Spirit activated: {spirit_id} ({spirit_name})")

    return JSONResponse({
        "status": "activated",
        "spirit_id": spirit_id,
        "spirit_name": spirit_name,
        "api_key": api_key  # One-time return - store this!
    })


async def handle_spirit_heartbeat(request: Request) -> JSONResponse:
    """Heartbeat from an active Spirit.

    POST /spirit/heartbeat
    Headers: Authorization: Bearer sk_egregore_...
    {
        "status": "idle" | "working" | "completing",
        "task_id": "..." (optional)
    }
    """
    # Validate API key
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer sk_egregore_"):
        return JSONResponse({"error": "Invalid authorization"}, status_code=401)

    api_key = auth_header.replace("Bearer ", "")
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Find Spirit by API key hash
    result = run_query("""
        MATCH (s:Spirit {apiKeyHash: $hash, status: "manifest"})
        SET s.lastHeartbeat = datetime()
        RETURN s.id AS id, s.name AS name
    """, {"hash": api_key_hash})

    if not result:
        return JSONResponse({"error": "Spirit not found or not active"}, status_code=404)

    spirit = result[0]

    try:
        data = await request.json()
        status = data.get("status", "idle")
    except Exception:
        status = "idle"

    logger.debug(f"Heartbeat from {spirit['name']}: {status}")

    return JSONResponse({
        "status": "ok",
        "spirit_id": spirit["id"]
    })


async def handle_spirit_callback(request: Request) -> JSONResponse:
    """Callback when a Spirit completes a task.

    POST /spirit/callback
    Headers: Authorization: Bearer sk_egregore_...
    {
        "task_id": "...",
        "status": "fulfilled" | "failed" | "timeout",
        "outputs": [...],
        "essence": {"tokens_in": N, "tokens_out": N, "model": "...", "cost_usd": N}
    }
    """
    # Validate API key
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer sk_egregore_"):
        return JSONResponse({"error": "Invalid authorization"}, status_code=401)

    api_key = auth_header.replace("Bearer ", "")
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Find Spirit by API key hash
    result = run_query("""
        MATCH (s:Spirit {apiKeyHash: $hash, status: "manifest"})-[:INVOKED_BY]->(p:Person)
        RETURN s.id AS spirit_id, s.name AS spirit_name, p.name AS vessel_name
    """, {"hash": api_key_hash})

    if not result:
        return JSONResponse({"error": "Spirit not found or not active"}, status_code=404)

    spirit_info = result[0]

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    task_id = data.get("task_id", "unknown")
    status = data.get("status", "fulfilled")
    outputs = data.get("outputs", [])
    essence = data.get("essence", {})

    # Log the task completion
    log_event(
        component="spirit",
        operation=f"task:{status}",
        tokens_in=essence.get("tokens_in", 0),
        tokens_out=essence.get("tokens_out", 0),
        model=essence.get("model", "unknown"),
        success=(status == "fulfilled"),
        user_name=spirit_info["vessel_name"],
        metadata={
            "spirit_id": spirit_info["spirit_id"],
            "spirit_name": spirit_info["spirit_name"],
            "task_id": task_id,
            "outputs_count": len(outputs),
            "cost_usd": essence.get("cost_usd", 0)
        }
    )

    # Update Spirit heartbeat
    run_query("""
        MATCH (s:Spirit {id: $id})
        SET s.lastHeartbeat = datetime()
    """, {"id": spirit_info["spirit_id"]})

    logger.info(f"Task callback from {spirit_info['spirit_name']}: {task_id} -> {status}")

    # TODO: Process outputs (create artifacts, sessions, etc.) based on trust level
    # For now, just acknowledge

    return JSONResponse({
        "status": "received",
        "task_id": task_id,
        "outputs_processed": len(outputs)
    })


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:
    """Start the bot."""
    global telegram_bot

    # Initialize capabilities
    check_graph_availability()
    init_memory_repos()

    ptb_app = Application.builder().token(BOT_TOKEN).build()
    telegram_bot = ptb_app.bot

    ptb_app.add_handler(CommandHandler("start", start_command))
    ptb_app.add_handler(CommandHandler("activity", activity_command))
    ptb_app.add_handler(CommandHandler("debug", debug_command))
    ptb_app.add_handler(CommandHandler("isolation", isolation_test_command))
    ptb_app.add_handler(CommandHandler("onboard", onboard_command))
    ptb_app.add_handler(ChatMemberHandler(handle_member_update, ChatMemberHandler.CHAT_MEMBER))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        logger.info(f"Starting webhook + notification API on port {PORT}")
        import asyncio

        async def handle_telegram_webhook(request: Request) -> PlainTextResponse:
            """Handle incoming Telegram webhook updates."""
            data = await request.json()
            update = Update.de_json(data, ptb_app.bot)
            await ptb_app.process_update(update)
            return PlainTextResponse("ok")

        async def health_check(request: Request) -> PlainTextResponse:
            """Health check endpoint."""
            return PlainTextResponse("ok")

        # Single Starlette app with all endpoints
        # Use token as webhook path for security (only Telegram knows it)
        webhook_path = f"/{BOT_TOKEN}"
        starlette_app = Starlette(
            routes=[
                Route(webhook_path, handle_telegram_webhook, methods=["POST"]),
                Route("/notify", handle_notify_request, methods=["POST"]),
                Route("/health", health_check, methods=["GET"]),
                # Spirit adapter endpoints
                Route("/spirit/init", handle_spirit_init, methods=["POST"]),
                Route("/spirit/activate", handle_spirit_activate, methods=["POST"]),
                Route("/spirit/heartbeat", handle_spirit_heartbeat, methods=["POST"]),
                Route("/spirit/callback", handle_spirit_callback, methods=["POST"]),
            ]
        )

        async def run_server():
            # Initialize PTB app
            await ptb_app.initialize()
            await ptb_app.start()

            # Set webhook (use token as path for security)
            webhook_url = f"https://{WEBHOOK_URL}/{BOT_TOKEN}"
            await ptb_app.bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook set to https://{WEBHOOK_URL}/[TOKEN]")

            # Start background memory sync
            if MEMORY_AVAILABLE:
                asyncio.create_task(sync_memory_repos_background())

            # Run uvicorn
            config = uvicorn.Config(starlette_app, host="0.0.0.0", port=PORT, log_level="info")
            server = uvicorn.Server(config)
            await server.serve()

        asyncio.run(run_server())
    else:
        logger.info("Starting polling mode...")
        # In polling mode, use simple run_polling
        ptb_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
