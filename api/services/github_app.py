"""GitHub App authentication and installation token management.

Generates short-lived installation tokens from the App's private key.
These tokens are scoped to repos the org admin selected during installation,
replacing the need for broad user-level `repo` scope.
"""

import os
import time
import logging

import httpx
import jwt

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"

# Loaded from environment on the API server
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_APP_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "")
GITHUB_APP_CLIENT_SECRET = os.environ.get("GITHUB_APP_CLIENT_SECRET", "")
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "egregore-labs")
_GITHUB_APP_PRIVATE_KEY_RAW = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")

# Cache: installation_id -> (token, expiry_timestamp)
_token_cache: dict[int, tuple[str, float]] = {}

# Cache: org_login (lowercase) -> installation_id
_installation_cache: dict[str, int] = {}


def is_configured() -> bool:
    """Check if GitHub App credentials are available."""
    return bool(GITHUB_APP_ID and _GITHUB_APP_PRIVATE_KEY_RAW)


def _load_private_key() -> str:
    """Load the PEM private key from env. Handles escaped newlines."""
    raw = _GITHUB_APP_PRIVATE_KEY_RAW
    if not raw:
        raise RuntimeError("GITHUB_APP_PRIVATE_KEY not configured")
    # Railway stores multiline as literal \n — unescape
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw


def _generate_jwt() -> str:
    """Generate a short-lived JWT (10 min) to authenticate as the GitHub App."""
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Clock skew tolerance
        "exp": now + (10 * 60),  # 10 minutes
        "iss": int(GITHUB_APP_ID),
    }
    pem = _load_private_key()
    return jwt.encode(payload, pem, algorithm="RS256")


def _app_headers() -> dict:
    """Headers for authenticating as the GitHub App (JWT)."""
    return {
        "Authorization": f"Bearer {_generate_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_installation_id(github_org: str) -> int | None:
    """Find the App installation ID for a GitHub org/user.

    Checks cache first, then queries GitHub API.
    Returns None if the App is not installed on this org.
    """
    key = github_org.lower()
    if key in _installation_cache:
        return _installation_cache[key]

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            f"{API_BASE}/app/installations",
            headers=_app_headers(),
            params={"per_page": 100},
            timeout=10.0,
        )

    if resp.status_code != 200:
        logger.warning(f"Failed to list App installations: {resp.status_code} {resp.text[:200]}")
        return None

    for inst in resp.json():
        account = inst.get("account", {})
        login = account.get("login", "").lower()
        _installation_cache[login] = inst["id"]
        if login == key:
            return inst["id"]

    return None


async def get_installation_token(installation_id: int) -> str:
    """Get an installation access token (1hr TTL). Uses cache when possible."""
    cached = _token_cache.get(installation_id)
    if cached:
        token, expiry = cached
        if time.time() < expiry - 300:  # Refresh if < 5 min remaining
            return token

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.post(
            f"{API_BASE}/app/installations/{installation_id}/access_tokens",
            headers=_app_headers(),
            timeout=10.0,
        )

    if resp.status_code != 201:
        raise RuntimeError(
            f"Failed to create installation token: {resp.status_code} {resp.text[:200]}"
        )

    data = resp.json()
    token = data["token"]
    expires_at = data.get("expires_at", "")
    if expires_at:
        from datetime import datetime
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            expiry = time.time() + 3600
    else:
        expiry = time.time() + 3600

    _token_cache[installation_id] = (token, expiry)
    return token


async def get_token_for_org(github_org: str) -> str | None:
    """Get an installation token for a GitHub org. Returns None if App not installed."""
    installation_id = await get_installation_id(github_org)
    if installation_id is None:
        return None
    return await get_installation_token(installation_id)


async def check_installation(github_org: str) -> dict:
    """Check if the GitHub App is installed on an org.

    Returns {"installed": True, "installation_id": 123}
    or {"installed": False, "install_url": "https://github.com/apps/..."}
    """
    installation_id = await get_installation_id(github_org)
    if installation_id:
        return {"installed": True, "installation_id": installation_id}

    install_url = f"https://github.com/apps/{GITHUB_APP_SLUG}/installations/new"
    return {"installed": False, "install_url": install_url}


def clear_cache():
    """Clear all caches. Useful after App reinstallation."""
    _token_cache.clear()
    _installation_cache.clear()
