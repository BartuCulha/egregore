"""
Egregore Coder Service — Coder API client for managing users and workspaces.

Each org's Coder instance runs on its own VPS. This service talks to
a specific Coder instance to create users, manage workspaces, and
push templates.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class CoderClient:
    """Client for a single Coder instance (one per org VPS)."""

    def __init__(self, base_url: str, session_token: str):
        self.base_url = base_url.rstrip("/")
        self.session_token = session_token

    def _headers(self) -> dict:
        return {
            "Coder-Session-Token": self.session_token,
            "Content-Type": "application/json",
        }

    async def health_check(self) -> dict:
        """Check if the Coder instance is healthy."""
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(f"{self.base_url}/api/v2/buildinfo")
                if resp.status_code == 200:
                    return {"healthy": True, **resp.json()}
                return {"healthy": False, "status": resp.status_code}
            except Exception as e:
                return {"healthy": False, "error": str(e)}

    async def create_user(
        self,
        username: str,
        email: str,
        name: str = "",
        password: Optional[str] = None,
    ) -> dict:
        """Create a Coder user. Returns user info or error."""
        async with httpx.AsyncClient(timeout=15) as client:
            # Check if user already exists
            try:
                check = await client.get(
                    f"{self.base_url}/api/v2/users/{username}",
                    headers=self._headers(),
                )
                if check.status_code == 200:
                    return {"status": "exists", "user": check.json()}
            except Exception:
                pass

            # Create user
            body = {
                "username": username,
                "email": email or f"{username}@users.noreply.github.com",
                "name": name or username,
                "login_type": "password",
                "disable_login": False,
            }
            if password:
                body["password"] = password

            resp = await client.post(
                f"{self.base_url}/api/v2/users",
                headers=self._headers(),
                json=body,
            )

            if resp.status_code in (200, 201):
                logger.info(f"Coder user created: {username}")
                return {"status": "created", "user": resp.json()}
            else:
                logger.warning(f"Coder user creation failed: {resp.status_code} {resp.text}")
                return {"status": "error", "detail": resp.text}

    async def list_users(self) -> list:
        """List all users on this Coder instance."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/v2/users",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                return resp.json().get("users", [])
            return []

    async def get_templates(self) -> list:
        """List templates on this Coder instance."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/v2/templates",
                headers=self._headers(),
            )
            if resp.status_code == 200:
                return resp.json()
            return []

    async def list_workspaces(self, owner: str = "") -> list:
        """List workspaces, optionally filtered by owner."""
        async with httpx.AsyncClient(timeout=15) as client:
            params = {}
            if owner:
                params["q"] = f"owner:{owner}"
            resp = await client.get(
                f"{self.base_url}/api/v2/workspaces",
                headers=self._headers(),
                params=params,
            )
            if resp.status_code == 200:
                return resp.json().get("workspaces", [])
            return []


async def get_coder_client(coder_url: str, session_token: str) -> CoderClient:
    """Create a CoderClient for an org's Coder instance.

    In production, the session_token is stored per-org in Supabase
    after VPS provisioning.
    """
    return CoderClient(coder_url, session_token)
