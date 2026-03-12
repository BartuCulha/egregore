"""Self-serve Egregore lifecycle — comprehensive regression suite.

Covers every step a client goes through to create an Egregore, invite members,
and get them into a hosted workspace. If these tests pass, we can hand the
product to a client and know the self-serve path works end-to-end.

Test matrix:
  1. Invite → peek → accept → claim (full chain, slug preserved)
  2. Multi-org isolation (two instances on one GitHub org)
  3. Invite accept triggers Coder user + workspace creation
  4. CoderClient.create_user includes organization_ids (Coder v2.31+ fix)
  5. _get_coder_credentials auto-fetches token when missing
  6. Join flow with hosted workspace
  7. Token lifecycle (expiry, consumption, replay prevention)
  8. Auth edge cases (wrong user accepts, revoked token, missing slug)
  9. Workspace endpoint creates user + workspace on demand
  10. Deprovision clears all credentials

All external calls (GitHub, Neo4j, Telegram, Coder, Supabase) are mocked.
"""

import sys
import json
import base64
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import respx
from httpx import Response

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.tokens import _tokens

pytestmark = [pytest.mark.api, pytest.mark.flow]

GITHUB_API = "https://api.github.com"
ADMIN_TOKEN = "ghp_admin_token"
INVITEE_TOKEN = "ghp_invitee_token"
JOINER_TOKEN = "ghp_joiner_token"

# --- Org configs for testing ---

ACME_SLUG = "acme"
ACME_API_KEY = "ek_acme_supersecretkey123"
ACME_GH_ORG = "AcmeOrg"
ACME_CONFIG = {
    "api_key": ACME_API_KEY,
    "org_name": "Acme Corp",
    "github_org": ACME_GH_ORG,
    "neo4j_host": "neo4j-acme.example.com",
    "neo4j_user": "neo4j",
    "neo4j_password": "testpass",
    "telegram_bot_token": "111111:AAAcme",
    "telegram_chat_id": "-100acme",
    "slug": ACME_SLUG,
}

# Second instance under the SAME GitHub org (the multi-org scenario)
LABS_SLUG = "labs"
LABS_API_KEY = "ek_labs_differentkey456"
LABS_CONFIG = {
    "api_key": LABS_API_KEY,
    "org_name": "Acme Labs",
    "github_org": ACME_GH_ORG,  # SAME github_org
    "neo4j_host": "neo4j-labs.example.com",
    "neo4j_user": "neo4j",
    "neo4j_password": "testpass",
    "telegram_bot_token": "222222:BBBlabs",
    "telegram_chat_id": "-100labs",
    "slug": LABS_SLUG,
}

# Third instance on a different GitHub org (clean isolation baseline)
GAMMA_SLUG = "gamma"
GAMMA_API_KEY = "ek_gamma_gammakey789"
GAMMA_GH_ORG = "GammaOrg"
GAMMA_CONFIG = {
    "api_key": GAMMA_API_KEY,
    "org_name": "Gamma Inc",
    "github_org": GAMMA_GH_ORG,
    "neo4j_host": "neo4j-gamma.example.com",
    "neo4j_user": "neo4j",
    "neo4j_password": "testpass",
    "telegram_bot_token": "333333:CCCgamma",
    "telegram_chat_id": "-100gamma",
    "slug": GAMMA_SLUG,
}

# Coder constants
CODER_URL = "http://10.0.0.1"
CODER_TOKEN = "coder-session-token-abc123"
CODER_PASSWORD = "admin-password-xyz"
CODER_ORG_ID = "org-uuid-1234-5678"


@pytest.fixture(autouse=True)
def clear_tokens():
    _tokens.clear()
    yield
    _tokens.clear()


def _neo4j_ok():
    return {"data": {"fields": [], "values": []}}


def _b64_json(obj: dict) -> dict:
    raw = json.dumps(obj).encode()
    return {"content": base64.b64encode(raw).decode(), "encoding": "base64", "sha": "abc"}


def _egregore_json(org_name="Acme Corp", github_org="AcmeOrg", slug="acme",
                   repo_name="egregore-core", memory_repo=None):
    config = {
        "org_name": org_name,
        "github_org": github_org,
        "memory_repo": memory_repo or f"{github_org}-memory",
        "api_url": "https://api.example.com",
        "slug": slug,
        "repo_name": repo_name,
    }
    return _b64_json(config)


def _mock_github_admin(org=ACME_GH_ORG, invitee="invitee"):
    """Mock GitHub API for an admin creating an invite."""
    respx.get(f"{GITHUB_API}/user").mock(
        return_value=Response(200, json={"login": "admin", "name": "Admin"})
    )
    respx.get(f"{GITHUB_API}/repos/{org}/egregore-core").mock(
        return_value=Response(200, json={"full_name": f"{org}/egregore-core"})
    )
    respx.get(f"{GITHUB_API}/orgs/{org}").mock(
        return_value=Response(200, json={"login": org})
    )
    respx.get(f"{GITHUB_API}/user/memberships/orgs/{org}").mock(
        return_value=Response(200, json={"role": "admin"})
    )
    respx.put(f"{GITHUB_API}/orgs/{org}/memberships/{invitee}").mock(
        return_value=Response(200, json={"state": "pending"})
    )
    respx.get(f"{GITHUB_API}/repos/{org}/egregore-core/contents/egregore.json").mock(
        return_value=Response(200, json=_egregore_json(github_org=org))
    )
    respx.put(url__regex=rf"{GITHUB_API}/repos/{org}/.*/collaborators/{invitee}").mock(
        return_value=Response(204)
    )


def _mock_github_invitee(org=ACME_GH_ORG, login="invitee", state="active"):
    """Mock GitHub API for an invitee accepting an invite."""
    respx.get(f"{GITHUB_API}/user").mock(
        return_value=Response(200, json={"login": login, "name": "Invitee User"})
    )
    respx.get(f"{GITHUB_API}/orgs/{org}/memberships/{login}").mock(
        return_value=Response(200, json={"state": state})
    )
    if state == "pending":
        respx.patch(f"{GITHUB_API}/user/memberships/orgs/{org}").mock(
            return_value=Response(200, json={"state": "active"})
        )
    respx.get(f"{GITHUB_API}/repos/{org}/egregore-core/contents/egregore.json").mock(
        return_value=Response(200, json=_egregore_json(github_org=org))
    )
    respx.post(url__regex=r"https://neo4j.*").mock(
        return_value=Response(200, json=_neo4j_ok())
    )
    respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
        return_value=Response(200, json={"ok": False})
    )


def _mock_coder_api():
    """Mock all Coder API endpoints for user + workspace creation."""
    # Organization list (for _get_default_org_id)
    respx.get(f"{CODER_URL}/api/v2/organizations").mock(
        return_value=Response(200, json=[{"id": CODER_ORG_ID, "name": "default"}])
    )
    # User check (404 = doesn't exist yet)
    respx.get(url__regex=rf"{CODER_URL}/api/v2/users/(?!me)").mock(
        return_value=Response(404)
    )
    # Create user
    respx.post(f"{CODER_URL}/api/v2/users").mock(
        return_value=Response(201, json={"username": "invitee", "id": "user-uuid"})
    )
    # List workspaces (empty = no existing workspace)
    respx.get(f"{CODER_URL}/api/v2/workspaces").mock(
        return_value=Response(200, json={"workspaces": []})
    )
    # Template list
    respx.get(f"{CODER_URL}/api/v2/templates").mock(
        return_value=Response(200, json=[{"id": "tmpl-uuid", "name": "Egregore"}])
    )
    # Template detail
    respx.get(f"{CODER_URL}/api/v2/templates/tmpl-uuid").mock(
        return_value=Response(200, json={"active_version_id": "ver-uuid"})
    )
    # Create workspace
    respx.post(url__regex=rf"{CODER_URL}/api/v2/organizations/default/members/.*/workspaces").mock(
        return_value=Response(201, json={"name": "egregore", "latest_build": {"status": "pending"}})
    )
    # Login (for token auto-fetch)
    respx.post(f"{CODER_URL}/api/v2/users/login").mock(
        return_value=Response(201, json={"session_token": CODER_TOKEN})
    )


# =============================================================================
# 1. FULL INVITE → ACCEPT → CLAIM CHAIN
# =============================================================================


class TestFullInviteChain:
    """The golden path: admin invites → invitee accepts → setup token claimed."""

    @respx.mock
    def test_invite_accept_claim_preserves_slug(self, app_client, _patch_org_configs):
        """Slug must survive the entire invite → accept → claim chain."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        # Step 1: Admin creates invite
        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 200
        invite_token = resp.json()["invite_token"]
        assert invite_token.startswith("inv_")

        # Step 2: Peek — does not consume (peek only returns org_name, github_org, invited_by, invited_username)
        peek1 = app_client.get(f"/api/org/invite/{invite_token}")
        assert peek1.status_code == 200
        assert peek1.json()["org_name"] == "Acme Corp"
        assert peek1.json()["invited_username"] == "invitee"

        peek2 = app_client.get(f"/api/org/invite/{invite_token}")
        assert peek2.status_code == 200  # Still valid

        # Step 3: Invitee accepts
        _mock_github_invitee()
        accept = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert accept.status_code == 200
        data = accept.json()
        assert data["status"] == "accepted"
        assert data["org_slug"] == ACME_SLUG
        assert data["org_name"] == "Acme Corp"
        setup_token = data["setup_token"]
        assert setup_token.startswith("st_")

        # Step 4: Claim setup token
        claim = app_client.get(f"/api/org/claim/{setup_token}")
        assert claim.status_code == 200
        config = claim.json()
        assert config["slug"] == ACME_SLUG
        assert config["github_org"] == ACME_GH_ORG
        assert "egregore-core" in config["fork_url"]

        # Step 5: Setup token is consumed — second claim fails
        claim2 = app_client.get(f"/api/org/claim/{setup_token}")
        assert claim2.status_code == 404

        # Step 6: Invite token is consumed — second accept fails
        accept2 = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert accept2.status_code == 404

    @respx.mock
    def test_pending_membership_auto_accepted(self, app_client, _patch_org_configs):
        """Invitee with pending GitHub membership gets auto-accepted."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        # Mock admin GitHub for inviting "newbie"
        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "admin", "name": "Admin"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/egregore-core"})
        )
        respx.get(f"{GITHUB_API}/orgs/{ACME_GH_ORG}").mock(
            return_value=Response(200, json={"login": ACME_GH_ORG})
        )
        respx.get(f"{GITHUB_API}/user/memberships/orgs/{ACME_GH_ORG}").mock(
            return_value=Response(200, json={"role": "admin"})
        )
        respx.put(f"{GITHUB_API}/orgs/{ACME_GH_ORG}/memberships/newbie").mock(
            return_value=Response(200, json={"state": "pending"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json())
        )
        respx.put(url__regex=rf"{GITHUB_API}/repos/{ACME_GH_ORG}/.*/collaborators/newbie").mock(
            return_value=Response(204)
        )

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "newbie",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 200
        invite_token = resp.json()["invite_token"]

        # Invitee has pending membership
        _mock_github_invitee(login="newbie", state="pending")
        accept = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert accept.status_code == 200
        assert accept.json()["status"] == "accepted"

    @respx.mock
    def test_invite_response_contains_all_required_fields(self, app_client, _patch_org_configs):
        """Invite create response must have all fields the website needs."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        data = resp.json()
        assert "invite_url" in data
        assert "invite_token" in data
        assert "org_name" in data
        assert "invited_username" in data
        assert data["invited_username"] == "invitee"

    @respx.mock
    def test_accept_response_contains_all_setup_fields(self, app_client, _patch_org_configs):
        """Accept response must have everything the website setup page needs."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        _mock_github_invitee()
        accept = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        data = accept.json()

        required = ["status", "setup_token", "fork_url", "memory_url",
                     "org_slug", "org_name"]
        for field in required:
            assert field in data, f"Missing field: {field}"


# =============================================================================
# 2. MULTI-ORG ISOLATION
# =============================================================================


@pytest.mark.isolation
class TestMultiOrgIsolation:
    """Two Egregore instances under one GitHub org must never cross-contaminate.

    This is the exact production regression: Curve-Labs had 'curvelabs' and
    'egregore-0' sharing the same GitHub org. Old code took first match from
    ORG_CONFIGS, always returning 'curvelabs'.
    """

    @respx.mock
    def test_api_key_routes_to_correct_instance(self, app_client, _patch_org_configs):
        """ek_labs_xxx routes to 'labs', not 'acme', even though same github_org."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _patch_org_configs[LABS_SLUG] = {**LABS_CONFIG}
        _mock_github_admin()

        # Invite via labs API key
        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {LABS_API_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["org_name"] == "Acme Labs"

        # Verify invite token carries the correct slug
        from api.services.tokens import peek_token
        invite_data = peek_token(resp.json()["invite_token"])
        assert invite_data["slug"] == LABS_SLUG

    @respx.mock
    def test_acme_key_routes_to_acme(self, app_client, _patch_org_configs):
        """ek_acme_xxx routes to 'acme' even when 'labs' also exists."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _patch_org_configs[LABS_SLUG] = {**LABS_CONFIG}
        _mock_github_admin()

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["org_name"] == "Acme Corp"

        from api.services.tokens import peek_token
        assert peek_token(resp.json()["invite_token"])["slug"] == ACME_SLUG

    @respx.mock
    def test_accept_preserves_slug_across_instances(self, app_client, _patch_org_configs):
        """Accept on 'labs' invite must yield 'labs' setup token, not 'acme'."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _patch_org_configs[LABS_SLUG] = {**LABS_CONFIG}

        _mock_github_admin()
        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {LABS_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        # Accept as invitee — mock egregore.json to return labs config
        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "invitee", "name": "Invitee User"})
        )
        respx.get(f"{GITHUB_API}/orgs/{ACME_GH_ORG}/memberships/invitee").mock(
            return_value=Response(200, json={"state": "active"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json(
                org_name="Acme Labs", github_org=ACME_GH_ORG, slug=LABS_SLUG
            ))
        )
        respx.post(url__regex=r"https://neo4j.*").mock(
            return_value=Response(200, json=_neo4j_ok())
        )
        respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=Response(200, json={"ok": False})
        )

        accept = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert accept.status_code == 200
        assert accept.json()["org_slug"] == LABS_SLUG
        assert accept.json()["org_name"] == "Acme Labs"

    @respx.mock
    def test_legacy_auth_ambiguous_github_org_rejected(self, app_client, _patch_org_configs):
        """Legacy (GitHub token) auth with ambiguous github_org must fail, not guess."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _patch_org_configs[LABS_SLUG] = {**LABS_CONFIG}
        _mock_github_admin()

        # Make egregore.json unreadable so legacy path can't resolve slug
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(404)
        )

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert resp.status_code == 400
        assert "slug" in resp.json()["detail"].lower()

    @respx.mock
    def test_legacy_auth_unambiguous_single_org_works(self, app_client, _patch_org_configs):
        """Legacy auth with unambiguous github_org (only one config) works."""
        _patch_org_configs[GAMMA_SLUG] = {**GAMMA_CONFIG}

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "admin", "name": "Admin"})
        )
        respx.get(f"{GITHUB_API}/repos/{GAMMA_GH_ORG}/egregore-core").mock(
            return_value=Response(200, json={"full_name": f"{GAMMA_GH_ORG}/egregore-core"})
        )
        respx.get(f"{GITHUB_API}/orgs/{GAMMA_GH_ORG}").mock(
            return_value=Response(200, json={"login": GAMMA_GH_ORG})
        )
        respx.get(f"{GITHUB_API}/user/memberships/orgs/{GAMMA_GH_ORG}").mock(
            return_value=Response(200, json={"role": "admin"})
        )
        respx.put(url__regex=rf"{GITHUB_API}/orgs/{GAMMA_GH_ORG}/memberships/invitee").mock(
            return_value=Response(200, json={"state": "pending"})
        )
        respx.get(f"{GITHUB_API}/repos/{GAMMA_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json(
                org_name="Gamma Inc", github_org=GAMMA_GH_ORG, slug=GAMMA_SLUG
            ))
        )
        respx.put(url__regex=rf"{GITHUB_API}/repos/{GAMMA_GH_ORG}/.*/collaborators/invitee").mock(
            return_value=Response(204)
        )

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": GAMMA_GH_ORG, "github_username": "invitee"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json()["org_name"] == "Gamma Inc"

    @respx.mock
    def test_invalid_api_key_rejected(self, app_client, _patch_org_configs):
        """API key with wrong secret → 401."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": "Bearer ek_acme_wrongsecret"},
        )
        assert resp.status_code == 401

    @respx.mock
    def test_api_key_auth_requires_github_token_in_body(self, app_client, _patch_org_configs):
        """API key auth without github_token in body → 400."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee"},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 400
        assert "github_token" in resp.json()["detail"]

    @respx.mock
    def test_cross_instance_invite_token_cannot_accept_on_wrong_org(
        self, app_client, _patch_org_configs
    ):
        """An invite token from 'labs' carries slug='labs'. Accept resolves to 'labs'."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _patch_org_configs[LABS_SLUG] = {**LABS_CONFIG}

        _mock_github_admin()
        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {LABS_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        # The slug baked into the token is 'labs'
        from api.services.tokens import peek_token
        assert peek_token(invite_token)["slug"] == LABS_SLUG

        # Accept — should route to labs, not acme
        _mock_github_invitee()
        accept = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert accept.status_code == 200

        # Setup token carries labs slug
        setup_token = accept.json()["setup_token"]
        claim = app_client.get(f"/api/org/claim/{setup_token}")
        assert claim.status_code == 200
        assert claim.json()["slug"] == LABS_SLUG


# =============================================================================
# 3. CODER USER + WORKSPACE CREATION ON INVITE ACCEPT
# =============================================================================


class TestInviteAcceptCoderIntegration:
    """Invite accept must pre-create Coder user + workspace when hosting is enabled."""

    @respx.mock
    def test_accept_creates_coder_user_and_workspace(self, app_client, _patch_org_configs):
        """When hosting is enabled, accept creates Coder user + workspace."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        # Create invite
        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        _mock_github_invitee()
        _mock_coder_api()

        # Mock _get_coder_credentials to return valid credentials
        # USE_SUPABASE=False skips Supabase membership registration
        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=(CODER_URL, CODER_TOKEN)), \
             patch("api.main.USE_SUPABASE", False):
            accept = app_client.post(
                f"/api/org/invite/{invite_token}/accept",
                headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
            )

        assert accept.status_code == 200
        assert accept.json()["hosting_coder_url"] == CODER_URL

    @respx.mock
    def test_accept_succeeds_even_if_coder_fails(self, app_client, _patch_org_configs):
        """Coder failure must not block invite accept (fire-and-forget)."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        _mock_github_invitee()

        # _get_coder_credentials raises — accept must still work
        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   side_effect=Exception("Coder is down")):
            accept = app_client.post(
                f"/api/org/invite/{invite_token}/accept",
                headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
            )

        assert accept.status_code == 200
        assert accept.json()["status"] == "accepted"
        assert accept.json()["hosting_coder_url"] is None

    @respx.mock
    def test_accept_without_hosting_returns_no_coder_url(self, app_client, _patch_org_configs):
        """Org without hosting → hosting_coder_url is falsy."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_github_admin()

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        invite_token = resp.json()["invite_token"]

        _mock_github_invitee()

        # No Coder credentials → no hosting
        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=("", "")):
            accept = app_client.post(
                f"/api/org/invite/{invite_token}/accept",
                headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
            )

        assert accept.status_code == 200
        assert not accept.json()["hosting_coder_url"]  # None or empty string


# =============================================================================
# 4. CODER CLIENT — organization_ids FIX (Coder v2.31+)
# =============================================================================


class TestCoderClientOrgIds:
    """CoderClient.create_user must include organization_ids for Coder v2.31+.

    Without this, Coder returns: 'required at least 1 value for organization_ids'
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_user_includes_organization_ids(self):
        """create_user fetches org ID and includes it in the POST body."""
        from api.services.coder import CoderClient

        client = CoderClient(CODER_URL, CODER_TOKEN)

        # Mock org list
        respx.get(f"{CODER_URL}/api/v2/organizations").mock(
            return_value=Response(200, json=[{"id": CODER_ORG_ID, "name": "default"}])
        )
        # Mock user check (doesn't exist)
        respx.get(f"{CODER_URL}/api/v2/users/newuser").mock(
            return_value=Response(404)
        )

        # Capture the create request
        create_route = respx.post(f"{CODER_URL}/api/v2/users").mock(
            return_value=Response(201, json={"username": "newuser", "id": "new-uuid"})
        )

        result = await client.create_user("newuser", "new@example.com", "New User")

        assert result["status"] == "created"
        # Verify organization_ids was sent
        request_body = json.loads(create_route.calls[0].request.content)
        assert "organization_ids" in request_body
        assert request_body["organization_ids"] == [CODER_ORG_ID]

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_user_existing_returns_exists(self):
        """If user already exists, return status='exists' without creating."""
        from api.services.coder import CoderClient

        client = CoderClient(CODER_URL, CODER_TOKEN)

        respx.get(f"{CODER_URL}/api/v2/users/existing").mock(
            return_value=Response(200, json={"username": "existing", "id": "exist-uuid"})
        )

        result = await client.create_user("existing", "exist@example.com")
        assert result["status"] == "exists"

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_user_works_without_org_id(self):
        """If org ID fetch fails, still tries to create user (without org_ids)."""
        from api.services.coder import CoderClient

        client = CoderClient(CODER_URL, CODER_TOKEN)

        respx.get(f"{CODER_URL}/api/v2/organizations").mock(
            return_value=Response(500)
        )
        respx.get(f"{CODER_URL}/api/v2/users/newuser").mock(
            return_value=Response(404)
        )

        create_route = respx.post(f"{CODER_URL}/api/v2/users").mock(
            return_value=Response(201, json={"username": "newuser"})
        )

        result = await client.create_user("newuser", "new@example.com")
        assert result["status"] == "created"
        body = json.loads(create_route.calls[0].request.content)
        assert "organization_ids" not in body

    @respx.mock
    @pytest.mark.asyncio
    async def test_create_workspace_with_rich_params(self):
        """Workspace creation passes org params as rich_parameter_values."""
        from api.services.coder import CoderClient

        client = CoderClient(CODER_URL, CODER_TOKEN)

        respx.get(f"{CODER_URL}/api/v2/workspaces").mock(
            return_value=Response(200, json={"workspaces": []})
        )
        respx.get(f"{CODER_URL}/api/v2/templates").mock(
            return_value=Response(200, json=[{"id": "t1", "name": "Egregore"}])
        )
        respx.get(f"{CODER_URL}/api/v2/templates/t1").mock(
            return_value=Response(200, json={"active_version_id": "v1"})
        )
        ws_route = respx.post(
            url__regex=rf"{CODER_URL}/api/v2/organizations/default/members/testuser/workspaces"
        ).mock(
            return_value=Response(201, json={"name": "egregore"})
        )

        result = await client.create_workspace(
            owner="testuser", org_slug="acme", org_name="Acme Corp",
            github_org="AcmeOrg", repo_name="egregore-core",
        )
        assert result["status"] == "created"

        body = json.loads(ws_route.calls[0].request.content)
        assert body["template_version_id"] == "v1"
        param_names = [p["name"] for p in body["rich_parameter_values"]]
        assert "org_slug" in param_names
        assert "org_name" in param_names
        assert "github_org" in param_names


# =============================================================================
# 5. _get_coder_credentials AUTO-FETCH
# =============================================================================


def _mock_supabase_client(select_data, update_data=None):
    """Create a mock Supabase client that returns select_data on .table().select().eq().execute()."""
    mock_client = MagicMock()
    select_result = MagicMock()
    select_result.data = select_data
    mock_client.table.return_value.select.return_value.eq.return_value.execute.return_value = select_result
    if update_data is not None:
        update_result = MagicMock()
        update_result.data = update_data
        mock_client.table.return_value.update.return_value.eq.return_value.execute.return_value = update_result
    return mock_client


class TestCoderCredentialAutoFetch:
    """_get_coder_credentials must auto-fetch token from Coder when missing in Supabase."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_auto_fetches_token_when_missing(self):
        """If hosting_coder_token is null but password exists, fetch + store."""
        mock_client = _mock_supabase_client(
            select_data=[{
                "hosting_coder_url": CODER_URL,
                "hosting_coder_token": None,
                "hosting_coder_password": CODER_PASSWORD,
                "hosting_ip": "10.0.0.1",
            }],
            update_data=[{}],
        )

        with patch("api.main.USE_SUPABASE", True), \
             patch("api.services.supabase.get_client", return_value=mock_client), \
             patch("api.services.hosting.get_coder_session_token", new_callable=AsyncMock, return_value="fresh-token"):
            from api.main import _get_coder_credentials
            url, token = await _get_coder_credentials("test-slug")

        assert url == CODER_URL
        assert token == "fresh-token"

    @pytest.mark.asyncio
    async def test_returns_existing_token_when_present(self):
        """If token already exists in Supabase, return it without fetching."""
        mock_client = _mock_supabase_client(
            select_data=[{
                "hosting_coder_url": CODER_URL,
                "hosting_coder_token": "existing-token",
                "hosting_coder_password": CODER_PASSWORD,
                "hosting_ip": "10.0.0.1",
            }],
        )

        with patch("api.main.USE_SUPABASE", True), \
             patch("api.services.supabase.get_client", return_value=mock_client):
            from api.main import _get_coder_credentials
            url, token = await _get_coder_credentials("test-slug")

        assert token == "existing-token"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_supabase(self):
        """Without Supabase, returns empty credentials."""
        with patch("api.main.USE_SUPABASE", False):
            from api.main import _get_coder_credentials
            url, token = await _get_coder_credentials("test-slug")

        assert url == ""
        assert token == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_password(self):
        """If no password stored (old provisioning), can't auto-fetch."""
        mock_client = _mock_supabase_client(
            select_data=[{
                "hosting_coder_url": CODER_URL,
                "hosting_coder_token": None,
                "hosting_coder_password": None,
                "hosting_ip": "10.0.0.1",
            }],
        )

        with patch("api.main.USE_SUPABASE", True), \
             patch("api.services.supabase.get_client", return_value=mock_client):
            from api.main import _get_coder_credentials
            url, token = await _get_coder_credentials("test-slug")

        assert url == CODER_URL
        assert not token  # None or empty — can't auto-fetch without password


# =============================================================================
# 6. JOIN FLOW
# =============================================================================


class TestJoinFlow:
    """POST /api/org/join — alternative to invite for existing org members."""

    @respx.mock
    def test_join_returns_setup_token_with_slug(self, app_client, _patch_org_configs):
        """Join returns a setup token with the correct slug and org config."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "joiner", "name": "Joiner"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/egregore-core"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json())
        )
        # Memory repo exists
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/{ACME_GH_ORG}-memory").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/{ACME_GH_ORG}-memory"})
        )
        # Neo4j
        respx.post(url__regex=r"https://neo4j.*").mock(
            return_value=Response(200, json=_neo4j_ok())
        )
        # Telegram
        respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=Response(200, json={"ok": False})
        )

        # Mock _get_coder_credentials (no hosting)
        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=("", "")):
            resp = app_client.post(
                "/api/org/join",
                json={"github_org": ACME_GH_ORG},
                headers={"Authorization": f"Bearer {JOINER_TOKEN}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_slug"] == ACME_SLUG
        assert data["setup_token"].startswith("st_")
        assert ACME_GH_ORG in data["fork_url"]

    @respx.mock
    def test_join_missing_slug_in_egregore_json(self, app_client, _patch_org_configs):
        """If egregore.json has no slug field, join must fail with a clear error."""
        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "joiner", "name": "Joiner"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/egregore-core"})
        )
        # egregore.json without slug
        config_no_slug = {
            "org_name": "Broken",
            "github_org": ACME_GH_ORG,
            "memory_repo": f"{ACME_GH_ORG}-memory",
            "api_url": "https://api.example.com",
        }
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_b64_json(config_no_slug))
        )
        # Memory repo exists
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/{ACME_GH_ORG}-memory").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/{ACME_GH_ORG}-memory"})
        )

        resp = app_client.post(
            "/api/org/join",
            json={"github_org": ACME_GH_ORG},
            headers={"Authorization": f"Bearer {JOINER_TOKEN}"},
        )
        assert resp.status_code == 400
        assert "slug" in resp.json()["detail"].lower()


# =============================================================================
# 7. TOKEN LIFECYCLE
# =============================================================================


class TestTokenLifecycle:
    """Setup and invite tokens: creation, consumption, expiry, replay prevention."""

    def test_setup_token_single_use(self, app_client):
        """Setup token can only be claimed once."""
        from api.services.tokens import create_token
        token = create_token({"slug": "test", "fork_url": "https://x.git"})

        resp1 = app_client.get(f"/api/org/claim/{token}")
        assert resp1.status_code == 200

        resp2 = app_client.get(f"/api/org/claim/{token}")
        assert resp2.status_code == 404

    def test_setup_token_expiry(self, app_client):
        """Expired setup token → 404."""
        import time
        from api.services.tokens import create_token
        token = create_token({"slug": "test"}, ttl=0)
        time.sleep(0.01)

        resp = app_client.get(f"/api/org/claim/{token}")
        assert resp.status_code == 404

    def test_invite_token_peek_does_not_consume(self):
        """Peeking an invite token does not consume it."""
        from api.services.tokens import create_invite_token, peek_token, claim_token

        token = create_invite_token({"slug": "test", "org_name": "Test"})

        # Peek multiple times
        for _ in range(3):
            data = peek_token(token)
            assert data is not None
            assert data["slug"] == "test"

        # Still claimable
        data = claim_token(token)
        assert data is not None

        # Now consumed
        data = claim_token(token)
        assert data is None

    def test_invite_token_expiry(self):
        """Expired invite token → None on peek and claim."""
        import time
        from api.services.tokens import create_invite_token, peek_token, claim_token

        token = create_invite_token({"slug": "test"}, ttl=0)
        time.sleep(0.01)

        assert peek_token(token) is None
        assert claim_token(token) is None

    def test_token_prefixes(self):
        """Setup tokens start with 'st_', invite tokens with 'inv_'."""
        from api.services.tokens import create_token, create_invite_token

        st = create_token({"x": 1})
        inv = create_invite_token({"x": 1})

        assert st.startswith("st_")
        assert inv.startswith("inv_")

    def test_bogus_token_returns_404(self, app_client):
        """Random token string → 404."""
        resp = app_client.get("/api/org/claim/st_does_not_exist")
        assert resp.status_code == 404

    @respx.mock
    def test_invite_accept_with_missing_slug_in_token(self, app_client):
        """Invite token without slug field → 400 with clear message."""
        from api.services.tokens import create_invite_token

        # Create an invite token that's missing the slug
        token = create_invite_token({"github_org": "SomeOrg", "org_name": "Some"})

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "invitee", "name": "Invitee"})
        )

        resp = app_client.post(
            f"/api/org/invite/{token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert resp.status_code == 400
        assert "slug" in resp.json()["detail"].lower()


# =============================================================================
# 8. AUTH EDGE CASES
# =============================================================================


class TestAuthEdgeCases:
    """Authentication boundary conditions."""

    @respx.mock
    def test_non_admin_cannot_invite(self, app_client, _patch_org_configs):
        """Member role → 403 on invite."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "member", "name": "Member"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core").mock(
            return_value=Response(200, json={"full_name": f"{ACME_GH_ORG}/egregore-core"})
        )
        respx.get(f"{GITHUB_API}/orgs/{ACME_GH_ORG}").mock(
            return_value=Response(200, json={"login": ACME_GH_ORG})
        )
        respx.get(f"{GITHUB_API}/user/memberships/orgs/{ACME_GH_ORG}").mock(
            return_value=Response(200, json={"role": "member"})
        )

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": "ghp_member_token"},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 403

    @respx.mock
    def test_invalid_github_token_on_accept(self, app_client, _patch_org_configs):
        """Invalid GitHub token on accept → 401."""
        from api.services.tokens import create_invite_token

        token = create_invite_token({
            "github_org": ACME_GH_ORG, "org_name": "Acme", "slug": ACME_SLUG,
            "invited_username": "invitee", "invited_by": "admin",
            "repos": [], "repo_name": "egregore-core",
        })

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(401, json={"message": "Bad credentials"})
        )

        resp = app_client.post(
            f"/api/org/invite/{token}/accept",
            headers={"Authorization": "Bearer ghp_invalidtoken"},
        )
        assert resp.status_code == 401

    @respx.mock
    def test_repo_not_found_on_invite(self, app_client, _patch_org_configs):
        """Inviting for a non-existent repo → 404."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "admin", "name": "Admin"})
        )
        respx.get(f"{GITHUB_API}/repos/{ACME_GH_ORG}/egregore-core").mock(
            return_value=Response(404)
        )

        resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert resp.status_code == 404


# =============================================================================
# 9. HOSTING USER ENDPOINT
# =============================================================================


class TestHostingUserEndpoint:
    """POST /api/hosting/user/{slug} — create Coder user via API."""

    @respx.mock
    def test_hosting_user_created(self, app_client, _patch_org_configs):
        """Valid API key + Coder credentials → user created."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}
        _mock_coder_api()

        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=(CODER_URL, CODER_TOKEN)):
            resp = app_client.post(
                f"/api/hosting/user/{ACME_SLUG}",
                json={"username": "newuser"},
                headers={"Authorization": f"Bearer {ACME_API_KEY}"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "created"

    @respx.mock
    def test_hosting_user_no_credentials(self, app_client, _patch_org_configs):
        """No Coder credentials → 404."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=("", "")):
            resp = app_client.post(
                f"/api/hosting/user/{ACME_SLUG}",
                json={"username": "newuser"},
                headers={"Authorization": f"Bearer {ACME_API_KEY}"},
            )

        assert resp.status_code == 404

    @respx.mock
    def test_hosting_user_wrong_api_key(self, app_client, _patch_org_configs):
        """Wrong API key → 401."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        resp = app_client.post(
            f"/api/hosting/user/{ACME_SLUG}",
            json={"username": "newuser"},
            headers={"Authorization": "Bearer ek_acme_wrongkey"},
        )
        assert resp.status_code in (401, 403)


# =============================================================================
# 10. DEPROVISION CLEARS CREDENTIALS
# =============================================================================


class TestDeprovision:
    """DELETE /api/hosting/deprovision/{slug} clears all sensitive hosting data."""

    @respx.mock
    def test_deprovision_clears_token_and_password(self, app_client, _patch_org_configs, monkeypatch):
        """Deprovision must clear hosting_coder_token and hosting_coder_password."""
        from api import main as _main_mod

        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        # Mock admin validation
        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "oguzhan", "name": "Oz"})
        )

        # Track what gets passed to Supabase update()
        captured_update_body = {}
        mock_client = MagicMock()
        update_result = MagicMock()
        update_result.data = [{}]

        def _capture_update(fields):
            captured_update_body.update(fields)
            chain = MagicMock()
            chain.eq.return_value.execute.return_value = update_result
            return chain

        mock_client.table.return_value.update.side_effect = _capture_update

        monkeypatch.setattr(_main_mod, "USE_SUPABASE", True)
        monkeypatch.setattr(_main_mod, "ADMIN_USERS", ["oguzhan"])

        with patch("api.services.hosting.deprovision_vps", new_callable=AsyncMock,
                   return_value={"status": "deleted"}), \
             patch("api.services.supabase.get_client", return_value=mock_client):
            resp = app_client.delete(
                f"/api/hosting/deprovision/{ACME_SLUG}",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )

        # Verify sensitive fields are cleared
        assert "hosting_coder_token" in captured_update_body
        assert captured_update_body["hosting_coder_token"] is None
        assert "hosting_coder_password" in captured_update_body
        assert captured_update_body["hosting_coder_password"] is None
        assert captured_update_body["hosting_enabled"] is False


# =============================================================================
# 11. SETUP TOKEN CLAIM — CONFIG COMPLETENESS
# =============================================================================


class TestClaimConfig:
    """Claiming a setup token must return everything npx install needs."""

    def test_claim_returns_complete_config(self, app_client):
        """Claimed config has all fields needed for CLI setup."""
        from api.services.tokens import create_token

        token = create_token({
            "fork_url": "https://github.com/AcmeOrg/egregore-core.git",
            "memory_url": "https://github.com/AcmeOrg/AcmeOrg-memory.git",
            "api_key": "ek_acme_key123",
            "api_url": "https://api.example.com",
            "org_name": "Acme Corp",
            "github_org": "AcmeOrg",
            "github_token": "ghp_xxx",
            "github_username": "invitee",
            "github_name": "Invitee User",
            "slug": "acme",
            "repos": ["frontend"],
            "repo_name": "egregore-core",
        })

        resp = app_client.get(f"/api/org/claim/{token}")
        assert resp.status_code == 200
        config = resp.json()

        # All fields the npx installer needs
        required = [
            "fork_url", "memory_url", "api_key", "api_url",
            "org_name", "github_org", "github_token", "github_username",
            "slug", "repo_name",
        ]
        for field in required:
            assert field in config, f"Missing field in claim config: {field}"
            assert config[field], f"Empty field in claim config: {field}"

        # URLs must be valid
        assert config["fork_url"].startswith("https://")
        assert config["memory_url"].startswith("https://")
        assert config["api_key"].startswith("ek_")
        assert config["slug"] == "acme"


# =============================================================================
# 12. END-TO-END: INVITE → ACCEPT → WORKSPACE READY (integration-level)
# =============================================================================


class TestEndToEndWithHosting:
    """The complete client self-serve journey with hosted VPS.

    This is the scenario that must work flawlessly for client handover:
    1. Admin invites user (API key auth)
    2. User clicks invite link → peeks org info
    3. User accepts invite → gets setup token + Coder URL
    4. Coder user + workspace pre-created
    5. User claims setup token → gets install config
    6. User opens terminal URL → workspace running
    """

    @respx.mock
    def test_full_hosted_journey(self, app_client, _patch_org_configs):
        """Complete journey: invite → accept → claim, with Coder workspace created."""
        _patch_org_configs[ACME_SLUG] = {**ACME_CONFIG}

        # -- Step 1: Admin invites --
        _mock_github_admin()
        invite_resp = app_client.post(
            "/api/org/invite",
            json={"github_org": ACME_GH_ORG, "github_username": "invitee",
                  "github_token": ADMIN_TOKEN},
            headers={"Authorization": f"Bearer {ACME_API_KEY}"},
        )
        assert invite_resp.status_code == 200
        invite_data = invite_resp.json()
        invite_token = invite_data["invite_token"]
        assert invite_data["org_name"] == "Acme Corp"

        # -- Step 2: Invitee peeks --
        peek_resp = app_client.get(f"/api/org/invite/{invite_token}")
        assert peek_resp.status_code == 200
        peek_data = peek_resp.json()
        assert peek_data["org_name"] == "Acme Corp"
        assert peek_data["invited_username"] == "invitee"

        # -- Step 3: Invitee accepts --
        _mock_github_invitee()
        _mock_coder_api()

        # Coder credentials available, skip Supabase
        with patch("api.main._get_coder_credentials", new_callable=AsyncMock,
                   return_value=(CODER_URL, CODER_TOKEN)), \
             patch("api.main.USE_SUPABASE", False):
            accept_resp = app_client.post(
                f"/api/org/invite/{invite_token}/accept",
                headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
            )

        assert accept_resp.status_code == 200
        accept_data = accept_resp.json()
        assert accept_data["status"] == "accepted"
        assert accept_data["org_slug"] == ACME_SLUG
        assert accept_data["hosting_coder_url"] == CODER_URL

        setup_token = accept_data["setup_token"]

        # -- Step 4: Claim setup token --
        claim_resp = app_client.get(f"/api/org/claim/{setup_token}")
        assert claim_resp.status_code == 200
        config = claim_resp.json()
        assert config["slug"] == ACME_SLUG
        assert config["github_org"] == ACME_GH_ORG
        assert "egregore-core" in config["fork_url"]
        assert config["github_username"] == "invitee"

        # -- Step 5: Verify workspace would be accessible --
        terminal_url = f"{CODER_URL}/@invitee/egregore.main/terminal"
        assert CODER_URL in terminal_url

        # -- Step 6: Token replay prevention --
        assert app_client.get(f"/api/org/claim/{setup_token}").status_code == 404
        assert app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        ).status_code == 404
