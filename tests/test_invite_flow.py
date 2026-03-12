"""Tests for the invitation lifecycle.

Admin invite, non-admin blocked, peek info, accept, consume, edge cases,
and multi-org slug isolation (cross-tenant safety).
All GitHub/Neo4j calls mocked with respx.
"""

import sys
import json
import base64
from pathlib import Path

import pytest
import respx
from httpx import Response

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.tokens import _tokens

pytestmark = pytest.mark.api

GITHUB_API = "https://api.github.com"
ADMIN_TOKEN = "ghp_admin_token"
MEMBER_TOKEN = "ghp_member_token"
INVITEE_TOKEN = "ghp_invitee_token"

# Shared GitHub org — two Egregore instances under one org
SHARED_GH_ORG = "SharedOrg"


@pytest.fixture(autouse=True)
def clear_tokens():
    _tokens.clear()
    yield
    _tokens.clear()


def _neo4j_ok():
    return {"data": {"fields": [], "values": []}}


def _egregore_json_content(org_name="Alpha Corp", github_org="AlphaOrg", slug="alpha"):
    config = {
        "org_name": org_name,
        "github_org": github_org,
        "memory_repo": f"{github_org}-memory",
        "api_url": "https://api.example.com",
        "slug": slug,
    }
    raw = json.dumps(config).encode()
    return {
        "content": base64.b64encode(raw).decode(),
        "encoding": "base64",
        "sha": "abc123",
    }


def _mock_admin_github():
    """Set up respx mocks for an admin user creating an invite."""
    respx.get(f"{GITHUB_API}/user").mock(
        return_value=Response(200, json={"login": "admin", "name": "Admin User"})
    )
    # Repo exists
    respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core").mock(
        return_value=Response(200, json={"full_name": "AlphaOrg/egregore-core"})
    )
    # AlphaOrg IS an org (not personal)
    respx.get(f"{GITHUB_API}/orgs/AlphaOrg").mock(
        return_value=Response(200, json={"login": "AlphaOrg"})
    )
    # Admin role
    respx.get(f"{GITHUB_API}/user/memberships/orgs/AlphaOrg").mock(
        return_value=Response(200, json={"role": "admin"})
    )
    # GitHub org invitation succeeds
    respx.put(f"{GITHUB_API}/orgs/AlphaOrg/memberships/invitee").mock(
        return_value=Response(200, json={"state": "pending"})
    )
    # egregore.json readable
    respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core/contents/egregore.json").mock(
        return_value=Response(200, json=_egregore_json_content())
    )
    # Add collaborator to egregore repo + memory repo
    respx.put(url__regex=rf"{GITHUB_API}/repos/AlphaOrg/.*/collaborators/invitee").mock(
        return_value=Response(204)
    )


# =============================================================================
# INVITE CREATION
# =============================================================================


class TestInviteCreation:
    @respx.mock
    def test_admin_can_invite(self, app_client, _patch_org_configs):
        """POST /api/org/invite with admin role returns invite_url and invite_token."""
        from conftest import ALPHA_SLUG, ALPHA_CONFIG
        _patch_org_configs[ALPHA_SLUG] = {**ALPHA_CONFIG}
        _mock_admin_github()

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": "AlphaOrg",
                "github_username": "invitee",
            },
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "invite_url" in data
        assert "invite_token" in data
        assert data["invite_token"].startswith("inv_")
        assert data["invited_username"] == "invitee"

    @respx.mock
    def test_non_admin_cannot_invite(self, app_client):
        """Member role → 403."""
        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "member", "name": "Member"})
        )
        respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core").mock(
            return_value=Response(200, json={"full_name": "AlphaOrg/egregore-core"})
        )
        # AlphaOrg IS an org
        respx.get(f"{GITHUB_API}/orgs/AlphaOrg").mock(
            return_value=Response(200, json={"login": "AlphaOrg"})
        )
        # Member role, not admin
        respx.get(f"{GITHUB_API}/user/memberships/orgs/AlphaOrg").mock(
            return_value=Response(200, json={"role": "member"})
        )

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": "AlphaOrg",
                "github_username": "someone",
            },
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )

        assert resp.status_code == 403


# =============================================================================
# INVITE PEEK
# =============================================================================


class TestInvitePeek:
    def test_invite_peek_shows_info(self, app_client):
        """GET /api/org/invite/{token} returns org name, inviter, without consuming."""
        from api.services.tokens import create_invite_token

        invite_data = {
            "github_org": "AlphaOrg",
            "org_name": "Alpha Corp",
            "invited_username": "invitee",
            "invited_by": "admin",
            "slug": "alpha",
            "repos": [],
            "repo_name": "egregore-core",
        }
        token = create_invite_token(invite_data)

        resp = app_client.get(f"/api/org/invite/{token}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "Alpha Corp"
        assert data["invited_by"] == "admin"
        assert data["invited_username"] == "invitee"

        # Token should NOT be consumed — peek again
        resp2 = app_client.get(f"/api/org/invite/{token}")
        assert resp2.status_code == 200

    def test_invite_peek_expired_returns_404(self, app_client):
        """Expired invite → 404."""
        from api.services.tokens import create_invite_token
        import time

        token = create_invite_token({"org_name": "X"}, ttl=0)
        time.sleep(0.01)

        resp = app_client.get(f"/api/org/invite/{token}")
        assert resp.status_code == 404


# =============================================================================
# INVITE ACCEPT
# =============================================================================


class TestInviteAccept:
    @respx.mock
    def test_accept_invite_with_active_membership(self, app_client, _patch_org_configs):
        """POST /api/org/invite/{token}/accept with active membership returns setup_token."""
        from api.services.tokens import create_invite_token
        from conftest import ALPHA_SLUG, ALPHA_CONFIG

        _patch_org_configs[ALPHA_SLUG] = {**ALPHA_CONFIG}

        invite_data = {
            "github_org": "AlphaOrg",
            "org_name": "Alpha Corp",
            "invited_username": "invitee",
            "invited_by": "admin",
            "slug": "alpha",
            "repos": [],
            "repo_name": "egregore-core",
        }
        invite_token = create_invite_token(invite_data)

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "invitee", "name": "Invitee"})
        )
        # Active membership
        respx.get(f"{GITHUB_API}/orgs/AlphaOrg/memberships/invitee").mock(
            return_value=Response(200, json={"state": "active"})
        )
        # egregore.json
        respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json_content())
        )
        # Neo4j person creation
        respx.post(url__regex=r"https://neo4j.*").mock(
            return_value=Response(200, json=_neo4j_ok())
        )
        # Telegram invite link
        respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=Response(200, json={"ok": False})
        )

        resp = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["setup_token"].startswith("st_")
        assert data["org_slug"] == "alpha"

    @respx.mock
    def test_accept_invite_auto_accepts_pending(self, app_client, _patch_org_configs):
        """Pending membership → auto-accepts, returns setup token."""
        from api.services.tokens import create_invite_token
        from conftest import ALPHA_SLUG, ALPHA_CONFIG

        _patch_org_configs[ALPHA_SLUG] = {**ALPHA_CONFIG}

        invite_data = {
            "github_org": "AlphaOrg",
            "org_name": "Alpha Corp",
            "invited_username": "invitee",
            "invited_by": "admin",
            "slug": "alpha",
            "repos": [],
            "repo_name": "egregore-core",
        }
        invite_token = create_invite_token(invite_data)

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "invitee", "name": "Invitee"})
        )
        # Pending membership
        respx.get(f"{GITHUB_API}/orgs/AlphaOrg/memberships/invitee").mock(
            return_value=Response(200, json={"state": "pending"})
        )
        # Accept invitation
        respx.patch(f"{GITHUB_API}/user/memberships/orgs/AlphaOrg").mock(
            return_value=Response(200, json={"state": "active"})
        )
        # egregore.json
        respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json_content())
        )
        # Neo4j
        respx.post(url__regex=r"https://neo4j.*").mock(
            return_value=Response(200, json=_neo4j_ok())
        )
        # Telegram
        respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=Response(200, json={"ok": False})
        )

        resp = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    @respx.mock
    def test_accept_invite_consumes_token(self, app_client, _patch_org_configs):
        """After accept, same invite token → 404."""
        from api.services.tokens import create_invite_token
        from conftest import ALPHA_SLUG, ALPHA_CONFIG

        _patch_org_configs[ALPHA_SLUG] = {**ALPHA_CONFIG}

        invite_data = {
            "github_org": "AlphaOrg",
            "org_name": "Alpha Corp",
            "invited_username": "invitee",
            "invited_by": "admin",
            "slug": "alpha",
            "repos": [],
            "repo_name": "egregore-core",
        }
        invite_token = create_invite_token(invite_data)

        respx.get(f"{GITHUB_API}/user").mock(
            return_value=Response(200, json={"login": "invitee", "name": "Invitee"})
        )
        respx.get(f"{GITHUB_API}/orgs/AlphaOrg/memberships/invitee").mock(
            return_value=Response(200, json={"state": "active"})
        )
        respx.get(f"{GITHUB_API}/repos/AlphaOrg/egregore-core/contents/egregore.json").mock(
            return_value=Response(200, json=_egregore_json_content())
        )
        respx.post(url__regex=r"https://neo4j.*").mock(
            return_value=Response(200, json=_neo4j_ok())
        )
        respx.post(url__regex=r"https://api\.telegram\.org/.*").mock(
            return_value=Response(200, json={"ok": False})
        )

        # First accept
        resp1 = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert resp1.status_code == 200

        # Second accept — invite token consumed
        resp2 = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert resp2.status_code == 404

    def test_accept_expired_invite_returns_404(self, app_client):
        """Expired invite token → 404."""
        from api.services.tokens import create_invite_token
        import time

        invite_token = create_invite_token({"github_org": "X", "slug": "x"}, ttl=0)
        time.sleep(0.01)

        resp = app_client.post(
            f"/api/org/invite/{invite_token}/accept",
            headers={"Authorization": f"Bearer {INVITEE_TOKEN}"},
        )
        assert resp.status_code == 404


# =============================================================================
# MULTI-ORG SLUG ISOLATION
# Regression guard: two Egregore instances sharing one GitHub org must never
# mix up slugs, Telegram groups, or VPS routing.
# =============================================================================


# Two orgs under the same GitHub org — the exact scenario that broke in production
INSTANCE_A_SLUG = "instance-a"
INSTANCE_A_API_KEY = "ek_instance-a_secretaaa111"
INSTANCE_A_CONFIG = {
    "api_key": INSTANCE_A_API_KEY,
    "org_name": "Instance A",
    "github_org": SHARED_GH_ORG,
    "neo4j_host": "neo4j-a.example.com",
    "neo4j_user": "neo4j",
    "neo4j_password": "testpass",
    "telegram_bot_token": "111111:AAA",
    "telegram_chat_id": "-100aaa",
    "slug": "instance-a",
}

INSTANCE_B_SLUG = "instance-b"
INSTANCE_B_API_KEY = "ek_instance-b_secretbbb222"
INSTANCE_B_CONFIG = {
    "api_key": INSTANCE_B_API_KEY,
    "org_name": "Instance B",
    "github_org": SHARED_GH_ORG,
    "neo4j_host": "neo4j-b.example.com",
    "neo4j_user": "neo4j",
    "neo4j_password": "testpass",
    "telegram_bot_token": "222222:BBB",
    "telegram_chat_id": "-100bbb",
    "slug": "instance-b",
}


def _mock_admin_github_shared_org(slug="instance-b", org_name="Instance B"):
    """Set up respx mocks for an admin inviting under the shared org."""
    respx.get(f"{GITHUB_API}/user").mock(
        return_value=Response(200, json={"login": "admin", "name": "Admin User"})
    )
    respx.get(f"{GITHUB_API}/repos/{SHARED_GH_ORG}/egregore-core").mock(
        return_value=Response(200, json={"full_name": f"{SHARED_GH_ORG}/egregore-core"})
    )
    respx.get(f"{GITHUB_API}/orgs/{SHARED_GH_ORG}").mock(
        return_value=Response(200, json={"login": SHARED_GH_ORG})
    )
    respx.get(f"{GITHUB_API}/user/memberships/orgs/{SHARED_GH_ORG}").mock(
        return_value=Response(200, json={"role": "admin"})
    )
    respx.put(f"{GITHUB_API}/orgs/{SHARED_GH_ORG}/memberships/invitee").mock(
        return_value=Response(200, json={"state": "pending"})
    )
    # egregore.json readable (for repos + memory_repo config)
    respx.get(f"{GITHUB_API}/repos/{SHARED_GH_ORG}/egregore-core/contents/egregore.json").mock(
        return_value=Response(200, json=_egregore_json_content(
            org_name=org_name, github_org=SHARED_GH_ORG, slug=slug,
        ))
    )
    # Collaborator additions
    respx.put(url__regex=rf"{GITHUB_API}/repos/{SHARED_GH_ORG}/.*/collaborators/invitee").mock(
        return_value=Response(204)
    )


@pytest.mark.isolation
@pytest.mark.flow
class TestMultiOrgSlugIsolation:
    """Two Egregore instances sharing one GitHub org must resolve to the correct slug.

    This is the regression that hit production: commit 70bb2bd changed slug
    resolution to iterate ORG_CONFIGS and take the first match for github_org.
    With two instances under Curve-Labs, it always returned 'curvelabs'
    instead of 'egregore-0'.
    """

    @respx.mock
    def test_api_key_resolves_correct_slug(self, app_client, _patch_org_configs):
        """API key ek_instance-b_xxx resolves to instance-b, not instance-a."""
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}
        _patch_org_configs[INSTANCE_B_SLUG] = {**INSTANCE_B_CONFIG}
        _mock_admin_github_shared_org()

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                "github_token": ADMIN_TOKEN,
            },
            headers={"Authorization": f"Bearer {INSTANCE_B_API_KEY}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "Instance B"

        # Verify the invite token carries the correct slug
        from api.services.tokens import peek_token
        invite_data = peek_token(data["invite_token"])
        assert invite_data["slug"] == "instance-b"

    @respx.mock
    def test_api_key_a_resolves_to_a_not_b(self, app_client, _patch_org_configs):
        """API key ek_instance-a_xxx resolves to instance-a."""
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}
        _patch_org_configs[INSTANCE_B_SLUG] = {**INSTANCE_B_CONFIG}
        _mock_admin_github_shared_org(slug="instance-a", org_name="Instance A")

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                "github_token": ADMIN_TOKEN,
            },
            headers={"Authorization": f"Bearer {INSTANCE_A_API_KEY}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "Instance A"

        from api.services.tokens import peek_token
        invite_data = peek_token(data["invite_token"])
        assert invite_data["slug"] == "instance-a"

    @respx.mock
    def test_invalid_api_key_rejected(self, app_client, _patch_org_configs):
        """API key with wrong secret is rejected."""
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                "github_token": ADMIN_TOKEN,
            },
            headers={"Authorization": "Bearer ek_instance-a_wrongsecret"},
        )

        assert resp.status_code == 401

    @respx.mock
    def test_api_key_without_github_token_rejected(self, app_client, _patch_org_configs):
        """API key auth without github_token in body → 400."""
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                # no github_token
            },
            headers={"Authorization": f"Bearer {INSTANCE_A_API_KEY}"},
        )

        assert resp.status_code == 400
        assert "github_token" in resp.json()["detail"]

    @respx.mock
    def test_legacy_github_token_ambiguous_org_returns_400(self, app_client, _patch_org_configs):
        """Legacy auth (GitHub token) with ambiguous github_org and no slug → 400.

        This is the exact scenario that silently returned the wrong org before.
        Now it must fail explicitly rather than guess.
        """
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}
        _patch_org_configs[INSTANCE_B_SLUG] = {**INSTANCE_B_CONFIG}

        _mock_admin_github_shared_org()
        # No egregore.json readable (repo doesn't have it or 404)
        respx.get(f"{GITHUB_API}/repos/{SHARED_GH_ORG}/egregore-core/contents/egregore.json").mock(
            return_value=Response(404)
        )

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                # No slug, no api_key — ambiguous
            },
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

        assert resp.status_code == 400
        assert "slug" in resp.json()["detail"].lower()

    @respx.mock
    def test_legacy_auth_with_explicit_slug_works(self, app_client, _patch_org_configs):
        """Legacy auth with explicit slug in body resolves correctly."""
        _patch_org_configs[INSTANCE_A_SLUG] = {**INSTANCE_A_CONFIG}
        _patch_org_configs[INSTANCE_B_SLUG] = {**INSTANCE_B_CONFIG}
        _mock_admin_github_shared_org()

        resp = app_client.post(
            "/api/org/invite",
            json={
                "github_org": SHARED_GH_ORG,
                "github_username": "invitee",
                "slug": "instance-b",
            },
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["org_name"] == "Instance B"

        from api.services.tokens import peek_token
        invite_data = peek_token(data["invite_token"])
        assert invite_data["slug"] == "instance-b"
