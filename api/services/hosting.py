"""
Egregore Hosting Service — VPS provisioning via Hetzner Cloud API.

Provisions one VPS per Egregore instance, installs Coder, deploys
the Egregore workspace template. Manages full lifecycle.
"""

import os
import logging
import secrets
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HETZNER_API_URL = "https://api.hetzner.cloud/v1"
HETZNER_TOKEN = os.environ.get("HETZNER_API_TOKEN", "")

# Default VPS tier for small teams (2 vCPU, 4GB RAM, ~$5/mo)
DEFAULT_SERVER_TYPE = "cx22"
DEFAULT_LOCATION = "fsn1"  # Falkenstein, Germany
DEFAULT_IMAGE = "ubuntu-22.04"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {HETZNER_TOKEN}",
        "Content-Type": "application/json",
    }


def _cloud_init_script(
    org_slug: str,
    coder_password: str,
    egregore_api_key: str,
    fork_url: str,
    memory_url: str,
    api_url: str,
    org_name: str,
    github_org: str,
    repo_name: str,
    managed_repos: str = "",
) -> str:
    """Generate cloud-init script that installs Coder + Egregore template on a fresh VPS."""
    return f"""#!/bin/bash
set -euo pipefail

# ─── System setup ────────────────────────────────────────────────
apt-get update
apt-get install -y docker.io docker-compose-plugin curl jq

systemctl enable docker
systemctl start docker

# ─── Install Coder ───────────────────────────────────────────────
curl -fsSL https://coder.com/install.sh | sh

# ─── Configure Coder ─────────────────────────────────────────────
mkdir -p /etc/coder.d

cat > /etc/coder.d/coder.env <<'CODERENV'
CODER_ACCESS_URL=https://$(curl -s http://169.254.169.254/hetzner/v1/metadata/public-ipv4)
CODER_WILDCARD_ACCESS_URL=
CODER_HTTP_ADDRESS=0.0.0.0:80
CODER_TLS_ENABLE=false
CODER_FIRST_USER_EMAIL=admin@egregore.xyz
CODER_FIRST_USER_USERNAME=admin
CODER_FIRST_USER_PASSWORD={coder_password}
CODER_FIRST_USER_TRIAL=false
CODER_TELEMETRY_ENABLE=false
CODERENV

# ─── Start Coder ─────────────────────────────────────────────────
systemctl enable coder
systemctl start coder

# Wait for Coder to be ready
for i in $(seq 1 30); do
  curl -sf http://localhost/api/v2/buildinfo && break
  sleep 2
done

# ─── Login and push template ─────────────────────────────────────
export CODER_URL="http://localhost"

coder login --first-user-email admin@egregore.xyz \
  --first-user-username admin \
  --first-user-password "{coder_password}" \
  --first-user-trial=false \
  "$CODER_URL" || true

# Pull the Egregore workspace image
docker pull ghcr.io/curve-labs/egregore-workspace:latest

# Store org config for template variables
mkdir -p /opt/egregore
cat > /opt/egregore/org-config.json <<'ORGCFG'
{{
  "org_slug": "{org_slug}",
  "org_name": "{org_name}",
  "github_org": "{github_org}",
  "repo_name": "{repo_name}",
  "fork_url": "{fork_url}",
  "memory_url": "{memory_url}",
  "api_url": "{api_url}",
  "managed_repos": "{managed_repos}",
  "egregore_api_key": "{egregore_api_key}"
}}
ORGCFG

echo "Egregore hosting ready for {org_slug}"
"""


async def provision_vps(
    org_slug: str,
    org_name: str,
    github_org: str,
    repo_name: str = "egregore-core",
    fork_url: str = "",
    memory_url: str = "",
    api_url: str = "",
    egregore_api_key: str = "",
    managed_repos: str = "",
    server_type: str = DEFAULT_SERVER_TYPE,
) -> dict:
    """Provision a Hetzner VPS with Coder installed for an org."""
    if not HETZNER_TOKEN:
        return {"error": "HETZNER_API_TOKEN not configured"}

    coder_password = secrets.token_urlsafe(24)

    cloud_init = _cloud_init_script(
        org_slug=org_slug,
        coder_password=coder_password,
        egregore_api_key=egregore_api_key,
        fork_url=fork_url or f"https://github.com/{github_org}/{repo_name}.git",
        memory_url=memory_url or f"https://github.com/{github_org}/{github_org}-memory.git",
        api_url=api_url,
        org_name=org_name,
        github_org=github_org,
        repo_name=repo_name,
        managed_repos=managed_repos,
    )

    server_name = f"egregore-{org_slug}"

    async with httpx.AsyncClient(timeout=60) as client:
        # Check if server already exists
        existing = await client.get(
            f"{HETZNER_API_URL}/servers",
            headers=_headers(),
            params={"name": server_name},
        )
        if existing.status_code == 200:
            servers = existing.json().get("servers", [])
            if servers:
                return {
                    "error": "VPS already exists",
                    "server_id": servers[0]["id"],
                    "ip": servers[0]["public_net"]["ipv4"]["ip"],
                }

        # Create SSH key if not exists (for admin access)
        # For now, rely on Coder's web terminal — no SSH key needed

        # Create the server
        resp = await client.post(
            f"{HETZNER_API_URL}/servers",
            headers=_headers(),
            json={
                "name": server_name,
                "server_type": server_type,
                "image": DEFAULT_IMAGE,
                "location": DEFAULT_LOCATION,
                "user_data": cloud_init,
                "labels": {
                    "service": "egregore",
                    "org": org_slug,
                },
                "start_after_create": True,
            },
        )

        if resp.status_code not in (200, 201):
            logger.error(f"Hetzner create failed: {resp.status_code} {resp.text}")
            return {"error": f"Hetzner API error: {resp.status_code}"}

        data = resp.json()
        server = data.get("server", {})
        server_id = server.get("id")
        ipv4 = server.get("public_net", {}).get("ipv4", {}).get("ip", "")

        logger.info(f"VPS provisioned: {server_name} ({server_id}) at {ipv4}")

        return {
            "status": "provisioning",
            "server_id": server_id,
            "server_name": server_name,
            "ip": ipv4,
            "coder_url": f"http://{ipv4}",
            "coder_password": coder_password,
            "org_slug": org_slug,
        }


async def get_vps_status(org_slug: str) -> dict:
    """Check VPS health and Coder readiness for an org."""
    if not HETZNER_TOKEN:
        return {"error": "HETZNER_API_TOKEN not configured"}

    server_name = f"egregore-{org_slug}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{HETZNER_API_URL}/servers",
            headers=_headers(),
            params={"name": server_name},
        )
        if resp.status_code != 200:
            return {"error": f"Hetzner API error: {resp.status_code}"}

        servers = resp.json().get("servers", [])
        if not servers:
            return {"status": "not_found"}

        server = servers[0]
        ip = server.get("public_net", {}).get("ipv4", {}).get("ip", "")
        hetzner_status = server.get("status", "unknown")

        result = {
            "status": hetzner_status,
            "server_id": server["id"],
            "ip": ip,
            "coder_url": f"http://{ip}",
            "server_type": server.get("server_type", {}).get("name", ""),
        }

        # Check if Coder is responding
        if hetzner_status == "running" and ip:
            try:
                coder_resp = await client.get(f"http://{ip}/api/v2/buildinfo", timeout=5)
                if coder_resp.status_code == 200:
                    result["coder_ready"] = True
                    result["coder_version"] = coder_resp.json().get("version", "")
                else:
                    result["coder_ready"] = False
            except Exception:
                result["coder_ready"] = False

        return result


async def deprovision_vps(org_slug: str) -> dict:
    """Tear down the VPS for an org."""
    if not HETZNER_TOKEN:
        return {"error": "HETZNER_API_TOKEN not configured"}

    server_name = f"egregore-{org_slug}"

    async with httpx.AsyncClient(timeout=30) as client:
        # Find server
        resp = await client.get(
            f"{HETZNER_API_URL}/servers",
            headers=_headers(),
            params={"name": server_name},
        )
        if resp.status_code != 200:
            return {"error": f"Hetzner API error: {resp.status_code}"}

        servers = resp.json().get("servers", [])
        if not servers:
            return {"status": "not_found"}

        server_id = servers[0]["id"]

        # Delete the server
        del_resp = await client.delete(
            f"{HETZNER_API_URL}/servers/{server_id}",
            headers=_headers(),
        )
        if del_resp.status_code not in (200, 204):
            return {"error": f"Delete failed: {del_resp.status_code}"}

        logger.info(f"VPS deprovisioned: {server_name} ({server_id})")
        return {"status": "deleted", "server_id": server_id}
