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
                "login_type": "github",
                "disable_login": False,
            }

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

    async def create_workspace(
        self,
        owner: str,
        template_name: str = "Egregore",
        workspace_name: str = "egregore",
        org_slug: str = "",
        org_name: str = "",
        github_org: str = "",
        repo_name: str = "egregore-core",
        managed_repos: str = "",
    ) -> dict:
        """Create a workspace for a user and start it.

        The workspace is pre-created so when the user clicks "Open in browser",
        it's already running — no "Create Workspace" button needed.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            # Check if workspace already exists for this user
            existing = await self.list_workspaces(owner=owner)
            if existing:
                return {"status": "exists", "workspace": existing[0]}

            # Find the template ID by name
            templates = await self.get_templates()
            template_id = None
            for t in templates:
                if t.get("name", "").lower() == template_name.lower():
                    template_id = t["id"]
                    break

            if not template_id:
                logger.warning(f"Template '{template_name}' not found on Coder instance")
                return {"status": "error", "detail": f"Template '{template_name}' not found"}

            # Get the active template version
            tmpl_resp = await client.get(
                f"{self.base_url}/api/v2/templates/{template_id}",
                headers=self._headers(),
            )
            if tmpl_resp.status_code != 200:
                return {"status": "error", "detail": f"Failed to get template: {tmpl_resp.status_code}"}
            active_version_id = tmpl_resp.json().get("active_version_id")

            # Build rich parameter values (org config for the workspace)
            rich_params = []
            if org_slug:
                rich_params.append({"name": "org_slug", "value": org_slug})
            if org_name:
                rich_params.append({"name": "org_name", "value": org_name})
            if github_org:
                rich_params.append({"name": "github_org", "value": github_org})
            if repo_name:
                rich_params.append({"name": "repo_name", "value": repo_name})
            rich_params.append({"name": "managed_repos", "value": managed_repos})

            body = {
                "name": workspace_name,
                "template_version_id": active_version_id,
            }
            if rich_params:
                body["rich_parameter_values"] = rich_params

            # Create workspace
            resp = await client.post(
                f"{self.base_url}/api/v2/organizations/default/members/{owner}/workspaces",
                headers=self._headers(),
                json=body,
            )

            if resp.status_code in (200, 201):
                logger.info(f"Coder workspace created: {owner}/{workspace_name}")
                return {"status": "created", "workspace": resp.json()}
            else:
                logger.warning(f"Workspace creation failed: {resp.status_code} {resp.text[:200]}")
                return {"status": "error", "detail": resp.text[:200]}


    async def create_user_token(self, username: str, lifetime_seconds: int = 600) -> str:
        """Create a short-lived API token for a user. Returns the token string or empty."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.base_url}/api/v2/users/{username}/keys",
                headers=self._headers(),
                json={"lifetime": lifetime_seconds * 1_000_000_000},  # nanoseconds
            )
            if resp.status_code in (200, 201):
                return resp.json().get("key", "")
            logger.warning(f"Token creation for {username} failed: {resp.status_code}")
            return ""


async def get_coder_client(coder_url: str, session_token: str) -> CoderClient:
    """Create a CoderClient for an org's Coder instance.

    In production, the session_token is stored per-org in Supabase
    after VPS provisioning.
    """
    return CoderClient(coder_url, session_token)
