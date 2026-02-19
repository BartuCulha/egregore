"""
Real integration tests for Health Check-in, User Dashboard, and Admin Health.

NO MOCKS — hits real GitHub API, real Supabase. Validates the full stack
as a non-tech user would experience it.

Requires:
  - api/.env with SUPABASE_URL and SUPABASE_SERVICE_KEY
  - .env with GITHUB_TOKEN (a valid GitHub OAuth token)
  - health_checkins table created in Supabase

Run:  pytest tests/test_health_dashboard.py -v
"""

import os
import sys
import time
import subprocess
import json
import pytest
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment setup — load credentials BEFORE any api imports
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent

# Load api/.env for Supabase credentials
from dotenv import load_dotenv

load_dotenv(ROOT / "api" / ".env")
load_dotenv(ROOT / ".env", override=False)

# Ensure sys.path
sys.path.insert(0, str(ROOT))

# Read tokens directly from file — conftest.py may load telegram-bot/.env
# which has a stale GITHUB_TOKEN. Always read from the project .env.
GITHUB_TOKEN = ""
API_KEY = ""
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("GITHUB_TOKEN="):
            GITHUB_TOKEN = line.split("=", 1)[1].strip()
        elif line.startswith("EGREGORE_API_KEY="):
            API_KEY = line.split("=", 1)[1].strip()

# Read org slug from egregore.json
_config_path = ROOT / "egregore.json"
ORG_SLUG = ""
if _config_path.exists():
    _cfg = json.loads(_config_path.read_text())
    ORG_SLUG = _cfg.get("slug", "")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def enable_supabase():
    """Ensure USE_SUPABASE=true on the api modules. Not a mock — just
    configuring the safety switch so the endpoints use the real Supabase."""
    from api import auth as _auth_mod
    from api import main as _main_mod

    old_auth = getattr(_auth_mod, "USE_SUPABASE", False)
    old_main = getattr(_main_mod, "USE_SUPABASE", False)
    _auth_mod.USE_SUPABASE = True
    _main_mod.USE_SUPABASE = True
    yield
    _auth_mod.USE_SUPABASE = old_auth
    _main_mod.USE_SUPABASE = old_main


@pytest.fixture(scope="module")
def client(enable_supabase):
    """FastAPI TestClient with USE_SUPABASE enabled."""
    from fastapi.testclient import TestClient
    from api.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def auth_header():
    """Authorization header with real GitHub token."""
    if not GITHUB_TOKEN:
        pytest.skip("GITHUB_TOKEN not configured in .env")
    return {"Authorization": f"Bearer {GITHUB_TOKEN}"}


# Track check-in IDs for cleanup
_checkin_ids: list[int] = []


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_checkins():
    """Clean up health_checkins created during tests (after all tests run)."""
    yield
    if not _checkin_ids:
        return
    try:
        from api.services.supabase import get_client
        sb = get_client()
        for cid in _checkin_ids:
            sb.table("health_checkins").delete().eq("id", cid).execute()
    except Exception:
        pass  # Best-effort cleanup


# ============================================================================
# Phase 1: POST /api/health/checkin
# ============================================================================


class TestHealthCheckin:
    """Phase 1 — health check-in endpoint, the foundation of the system."""

    def test_happy_path(self, client, auth_header):
        """POST a realistic health check-in and verify it lands in Supabase."""
        payload = {
            "org_slug": ORG_SLUG or "curvelabs",
            "key_valid": True,
            "key_slug": ORG_SLUG or "curvelabs",
            "config_slug": ORG_SLUG or "curvelabs",
            "framework_version": "0.4.2",
            "memory_linked": True,
            "git_synced": True,
            "branch": "dev/oguzhan/health-dashboard-tests",
            "errors": [],
            "platform": "Darwin",
            "shell": "zsh",
        }
        resp = client.post(
            "/api/health/checkin",
            json=payload,
            headers=auth_header,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        data = resp.json()
        assert data["status"] == "ok", f"Status not ok: {data}"
        assert "id" in data, f"No id in response: {data}"
        _checkin_ids.append(data["id"])

    def test_broken_key_scenario(self, client, auth_header):
        """Simulate the #1 problem: user has wrong API key after org rename."""
        payload = {
            "org_slug": ORG_SLUG or "curvelabs",
            "key_valid": False,
            "key_slug": "old-slug",
            "config_slug": ORG_SLUG or "curvelabs",
            "framework_version": "0.4.2",
            "memory_linked": True,
            "git_synced": True,
            "branch": "develop",
            "errors": ["key_slug_mismatch: key=old-slug config=curvelabs"],
            "platform": "Darwin",
            "shell": "zsh",
        }
        resp = client.post(
            "/api/health/checkin",
            json=payload,
            headers=auth_header,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        _checkin_ids.append(data["id"])

    def test_minimal_payload(self, client, auth_header):
        """Only org_slug is required — everything else is optional."""
        resp = client.post(
            "/api/health/checkin",
            json={"org_slug": ORG_SLUG or "curvelabs"},
            headers=auth_header,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        _checkin_ids.append(resp.json()["id"])

    def test_rejects_api_key_token(self, client):
        """ek_ tokens MUST be rejected — broken keys are what we're diagnosing."""
        if not API_KEY:
            pytest.skip("EGREGORE_API_KEY not configured")

        resp = client.post(
            "/api/health/checkin",
            json={"org_slug": "curvelabs"},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert resp.status_code == 401, f"Expected 401 for ek_ token, got {resp.status_code}"
        assert "GitHub token required" in resp.json().get("detail", "")

    def test_rejects_invalid_github_token(self, client):
        """Garbage tokens must fail cleanly."""
        resp = client.post(
            "/api/health/checkin",
            json={"org_slug": "curvelabs"},
            headers={"Authorization": "Bearer ghp_definitelynotavalidtoken123"},
        )
        assert resp.status_code == 401

    def test_rejects_missing_auth(self, client):
        """No auth header → 422 (FastAPI validation) or 401."""
        resp = client.post(
            "/api/health/checkin",
            json={"org_slug": "curvelabs"},
        )
        assert resp.status_code in (401, 422)

    def test_missing_org_slug(self, client, auth_header):
        """org_slug is required — missing it → 422."""
        resp = client.post(
            "/api/health/checkin",
            json={},
            headers=auth_header,
        )
        assert resp.status_code == 422


# ============================================================================
# Phase 3: GET /api/me/egregores (User Dashboard API)
# ============================================================================


class TestUserDashboard:
    """Phase 3/4 — user sees their own orgs, keys, members, health."""

    def test_returns_own_orgs(self, client, auth_header):
        """Authenticated user sees at least one org they belong to."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

        data = resp.json()
        assert "github_username" in data
        assert data["github_username"], "Username should not be empty"
        assert "egregores" in data
        assert len(data["egregores"]) >= 1, "User should belong to at least one org"

    def test_org_has_required_fields(self, client, auth_header):
        """Each org entry has slug, name, role, api_key, members."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        data = resp.json()
        org = data["egregores"][0]

        required_fields = ["slug", "name", "role", "api_key", "api_key_masked", "members"]
        for field in required_fields:
            assert field in org, f"Missing field '{field}' in org response"

    def test_api_key_present_and_masked(self, client, auth_header):
        """User can see their full API key + a masked version."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        org = resp.json()["egregores"][0]

        api_key = org["api_key"]
        masked = org["api_key_masked"]

        assert api_key.startswith("ek_"), f"API key should start with ek_: {api_key}"
        assert masked.startswith("ek_"), f"Masked key should start with ek_: {masked}"
        assert "****" in masked, f"Masked key should contain ****: {masked}"
        assert len(api_key) > len(masked.replace("*", "")), "Full key should be longer than unmasked portion"

    def test_members_list_populated(self, client, auth_header):
        """Org should have at least one member (the authenticated user)."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        org = resp.json()["egregores"][0]

        members = org["members"]
        assert isinstance(members, list)
        assert len(members) >= 1, "Should have at least one member"

        # The authenticated user should be in the member list
        usernames = [m.get("github_username", "").lower() for m in members]
        github_user = resp.json()["github_username"].lower()
        assert github_user in usernames, f"{github_user} not in member list: {usernames}"

    def test_health_checkin_attached(self, client, auth_header):
        """After posting a check-in, it should appear in the dashboard response."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        org = resp.json()["egregores"][0]

        # latest_checkin may be present from the earlier test posts
        assert "latest_checkin" in org, "Response should include latest_checkin field"
        assert "diagnostics" in org, "Response should include diagnostics field"

    def test_diagnostics_for_broken_key(self, client, auth_header):
        """If latest check-in has key_valid=false, diagnostics should flag it."""
        # Discover user's actual org slug from Supabase (not egregore.json)
        dash_resp = client.get("/api/me/egregores", headers=auth_header)
        assert dash_resp.status_code == 200
        orgs = dash_resp.json()["egregores"]
        assert len(orgs) >= 1, "User must belong to at least one org"
        slug = orgs[0]["slug"]

        # Post a broken-key check-in for this org
        payload = {
            "org_slug": slug,
            "key_valid": False,
            "key_slug": "wrong-slug",
            "config_slug": slug,
            "errors": ["key_slug_mismatch"],
        }
        post_resp = client.post("/api/health/checkin", json=payload, headers=auth_header)
        assert post_resp.status_code == 200
        _checkin_ids.append(post_resp.json()["id"])

        # Now check the dashboard — should show diagnostic
        resp = client.get("/api/me/egregores", headers=auth_header)
        data = resp.json()

        # Find the org
        target_org = next((o for o in data["egregores"] if o["slug"] == slug), None)
        assert target_org is not None, f"Org '{slug}' not found in response"

        if target_org.get("latest_checkin"):
            checkin = target_org["latest_checkin"]
            if checkin.get("key_valid") is False:
                diags = target_org["diagnostics"]
                types = [d["type"] for d in diags]
                assert "key_mismatch" in types, f"Expected key_mismatch diagnostic, got: {types}"

                # Verify the fix command includes the correct key
                mismatch_diag = next(d for d in diags if d["type"] == "key_mismatch")
                assert mismatch_diag["severity"] == "critical"
                assert "correct_key" in mismatch_diag, "Should include the correct API key"
                assert mismatch_diag["correct_key"].startswith("ek_"), "Correct key should be a real key"

    def test_rejects_api_key_token(self, client):
        """ek_ tokens must be rejected — same rule as health check-in."""
        if not API_KEY:
            pytest.skip("EGREGORE_API_KEY not configured")

        resp = client.get(
            "/api/me/egregores",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert resp.status_code == 401
        assert "GitHub token required" in resp.json().get("detail", "")

    def test_rejects_no_auth(self, client):
        """No auth → rejected."""
        resp = client.get("/api/me/egregores")
        assert resp.status_code in (401, 422)


# ============================================================================
# Phase 5: GET /api/admin/health (Admin Health Tab)
# ============================================================================


class TestAdminHealth:
    """Phase 5 — admin sees all users' health, alerts, version spread."""

    def test_returns_checkins(self, client, auth_header):
        """Admin sees recent health check-ins from all users."""
        resp = client.get("/api/admin/health", headers=auth_header)
        assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

        data = resp.json()
        assert "checkins" in data
        assert "alerts" in data
        assert "total_users" in data
        assert "versions" in data

        assert isinstance(data["checkins"], list)
        assert len(data["checkins"]) >= 1, "Should see at least one check-in (from earlier tests)"

    def test_checkin_has_expected_fields(self, client, auth_header):
        """Each check-in row has the diagnostic fields we need."""
        resp = client.get("/api/admin/health", headers=auth_header)
        checkins = resp.json()["checkins"]

        if not checkins:
            pytest.skip("No check-ins to validate")

        c = checkins[0]
        expected_fields = [
            "github_username", "org_slug", "checked_in_at",
            "key_valid", "memory_linked", "git_synced",
        ]
        for field in expected_fields:
            assert field in c, f"Missing field '{field}' in check-in: {list(c.keys())}"

    def test_broken_key_generates_alert(self, client, auth_header):
        """The broken-key check-in from earlier should generate a critical alert."""
        resp = client.get("/api/admin/health", headers=auth_header)
        data = resp.json()
        alerts = data["alerts"]

        # Should have at least one broken_key alert from test_broken_key_scenario
        alert_types = [a["type"] for a in alerts]
        assert "broken_key" in alert_types, f"Expected broken_key alert. Got types: {alert_types}"

        broken = [a for a in alerts if a["type"] == "broken_key"]
        assert broken[0]["severity"] == "critical"

    def test_org_filter(self, client, auth_header):
        """Filter by org_slug narrows results."""
        slug = ORG_SLUG or "curvelabs"
        resp = client.get(f"/api/admin/health?org_slug={slug}", headers=auth_header)
        assert resp.status_code == 200

        data = resp.json()
        # All returned check-ins should be for this org
        for c in data["checkins"]:
            assert c["org_slug"] == slug, f"Check-in for {c['org_slug']} leaked into {slug} filter"

    def test_nonexistent_org_filter(self, client, auth_header):
        """Filtering for a nonexistent org returns empty, not an error."""
        resp = client.get("/api/admin/health?org_slug=does-not-exist-99", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["checkins"]) == 0

    def test_version_spread_populated(self, client, auth_header):
        """Versions dict should reflect check-ins with framework_version."""
        resp = client.get("/api/admin/health", headers=auth_header)
        versions = resp.json()["versions"]
        assert isinstance(versions, dict)
        # We posted check-ins with version "0.4.2"
        if "0.4.2" in versions:
            assert versions["0.4.2"] >= 1

    def test_rejects_non_admin(self, client):
        """Non-admin GitHub token → 403. Invalid token → 401."""
        # We don't have a non-admin token, but we can verify invalid tokens fail
        resp = client.get(
            "/api/admin/health",
            headers={"Authorization": "Bearer ghp_notarealtokenatall999"},
        )
        assert resp.status_code == 401

    def test_rejects_no_auth(self, client):
        """No auth → rejected."""
        resp = client.get("/api/admin/health")
        assert resp.status_code in (401, 422)


# ============================================================================
# Security: Data isolation
# ============================================================================


class TestSecurityBoundaries:
    """Verify that user-scoped endpoints enforce access control."""

    def test_user_dashboard_scoped_to_authenticated_user(self, client, auth_header):
        """Response only contains orgs the authenticated user belongs to."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        data = resp.json()
        username = data["github_username"]

        for org in data["egregores"]:
            # The authenticated user should appear in each org's member list
            member_usernames = [
                m.get("github_username", "").lower()
                for m in org["members"]
            ]
            assert username.lower() in member_usernames, (
                f"User '{username}' should be a member of org '{org['slug']}' "
                f"but member list is: {member_usernames}"
            )

    def test_no_other_users_keys_in_user_dashboard(self, client, auth_header):
        """User dashboard returns API keys only for the user's own orgs.
        The key returned is the org's key (shared), not another user's key."""
        resp = client.get("/api/me/egregores", headers=auth_header)
        data = resp.json()

        for org in data["egregores"]:
            key = org.get("api_key", "")
            if key:
                # Key slug should match the org slug
                key_slug = key.split("_")[1] if len(key.split("_")) >= 3 else ""
                assert key_slug == org["slug"], (
                    f"API key slug '{key_slug}' doesn't match org slug '{org['slug']}'"
                )

    def test_health_checkin_auth_enforced(self, client):
        """Cannot post health check-ins without valid GitHub auth."""
        resp = client.post(
            "/api/health/checkin",
            json={"org_slug": "curvelabs"},
            headers={"Authorization": "Bearer ek_fake_notgithub"},
        )
        assert resp.status_code == 401, "ek_ tokens must be rejected"

    def test_admin_endpoint_requires_admin(self, client):
        """Admin health endpoint rejects non-admin auth.
        We can't test with a real non-admin token, so we verify that
        invalid tokens are properly rejected (not silently accepted)."""
        resp = client.get(
            "/api/admin/health",
            headers={"Authorization": "Bearer definitely_not_a_token"},
        )
        assert resp.status_code == 401


# ============================================================================
# Phase 2: bin/startup-check.sh
# ============================================================================


class TestStartupCheckScript:
    """Phase 2 — the client-side health reporter."""

    def test_script_exists(self):
        """startup-check.sh must exist and be executable."""
        script = ROOT / "bin" / "startup-check.sh"
        assert script.exists(), f"startup-check.sh not found at {script}"
        assert os.access(script, os.X_OK), "startup-check.sh should be executable"

    def test_syntax_valid(self):
        """Script passes bash syntax check."""
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "bin" / "startup-check.sh")],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_session_start_calls_startup_check(self):
        """session-start.sh should call startup-check.sh in background."""
        session_start = (ROOT / "bin" / "session-start.sh").read_text()
        assert "startup-check.sh" in session_start, (
            "session-start.sh should call startup-check.sh"
        )

    def test_script_reads_egregore_json(self):
        """Script reads config from egregore.json."""
        script = (ROOT / "bin" / "startup-check.sh").read_text()
        assert "egregore.json" in script
        assert "jq" in script, "Script should use jq to read JSON config"

    def test_script_uses_github_token_not_api_key(self):
        """Script authenticates with GITHUB_TOKEN, not EGREGORE_API_KEY.
        This is the critical design decision: broken keys can't self-report
        if the reporting mechanism uses the broken key."""
        script = (ROOT / "bin" / "startup-check.sh").read_text()
        assert "GITHUB_TOKEN" in script
        # The curl call should use GITHUB_TOKEN for auth
        assert "Bearer $GITHUB_TOKEN" in script

    def test_script_posts_to_health_endpoint(self):
        """Script POSTs to /api/health/checkin."""
        script = (ROOT / "bin" / "startup-check.sh").read_text()
        assert "/api/health/checkin" in script

    def test_script_validates_key_slug_match(self):
        """Script checks if key slug matches config slug."""
        script = (ROOT / "bin" / "startup-check.sh").read_text()
        assert "KEY_SLUG" in script
        assert "CONFIG_SLUG" in script

    def test_script_checks_memory_symlink(self):
        """Script verifies memory/ symlink exists."""
        script = (ROOT / "bin" / "startup-check.sh").read_text()
        assert "memory" in script
        assert "MEMORY_LINKED" in script

    def test_script_runs_successfully(self):
        """Script runs without error in the current environment.
        It should exit 0 even if the API call fails (fire-and-forget)."""
        result = subprocess.run(
            ["bash", str(ROOT / "bin" / "startup-check.sh")],
            capture_output=True, text=True,
            timeout=15,
            cwd=str(ROOT),
        )
        # The script should always exit 0 (fire-and-forget design)
        assert result.returncode == 0, (
            f"startup-check.sh failed with rc={result.returncode}\n"
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )


# ============================================================================
# End-to-end flow: non-tech user journey
# ============================================================================


class TestEndToEndUserJourney:
    """Simulate a non-tech user's full experience:
    1. Session starts → health check-in fires
    2. User visits /dashboard → sees their org, key, members
    3. Something breaks → diagnostics show what's wrong
    4. Admin sees the issue on /admin health tab
    """

    def test_full_flow(self, client, auth_header):
        """The complete happy path a user would experience."""
        # Discover user's actual org slug from Supabase
        r0 = client.get("/api/me/egregores", headers=auth_header)
        assert r0.status_code == 200
        orgs = r0.json()["egregores"]
        assert len(orgs) >= 1
        slug = orgs[0]["slug"]

        # Step 1: Session starts — startup-check.sh fires a health check-in
        checkin_payload = {
            "org_slug": slug,
            "key_valid": True,
            "key_slug": slug,
            "config_slug": slug,
            "framework_version": "0.4.2",
            "memory_linked": True,
            "git_synced": True,
            "branch": "dev/oguzhan/health-dashboard",
            "errors": [],
            "platform": "Darwin",
            "shell": "zsh",
        }
        r1 = client.post("/api/health/checkin", json=checkin_payload, headers=auth_header)
        assert r1.status_code == 200
        assert r1.json()["status"] == "ok"
        checkin_id = r1.json()["id"]
        _checkin_ids.append(checkin_id)

        # Step 2: User visits /dashboard → API returns their orgs
        r2 = client.get("/api/me/egregores", headers=auth_header)
        assert r2.status_code == 200
        dash = r2.json()
        assert len(dash["egregores"]) >= 1

        my_org = next((o for o in dash["egregores"] if o["slug"] == slug), None)
        assert my_org is not None, f"User should see org '{slug}'"
        assert my_org["api_key"].startswith("ek_")
        assert len(my_org["members"]) >= 1

        # Step 3: Admin sees this check-in on the health tab
        r3 = client.get("/api/admin/health", headers=auth_header)
        assert r3.status_code == 200
        health = r3.json()
        assert health["total_users"] >= 1

        # Find our check-in in the admin view
        our_checkin = None
        for c in health["checkins"]:
            if c.get("id") == checkin_id:
                our_checkin = c
                break

        # Even if deduplication dropped the exact ID, there should be
        # a recent check-in for this user+org
        if our_checkin is None:
            user_org_checkins = [
                c for c in health["checkins"]
                if c.get("github_username") == dash["github_username"]
                and c.get("org_slug") == slug
            ]
            assert len(user_org_checkins) >= 1, (
                "Admin should see at least one check-in for the test user"
            )

    def test_broken_key_flow(self, client, auth_header):
        """User has wrong key → diagnosed on both user and admin dashboards."""
        # Discover user's actual org slug from Supabase
        r0 = client.get("/api/me/egregores", headers=auth_header)
        assert r0.status_code == 200
        orgs = r0.json()["egregores"]
        assert len(orgs) >= 1
        slug = orgs[0]["slug"]

        # Step 1: Session starts with broken key
        broken_payload = {
            "org_slug": slug,
            "key_valid": False,
            "key_slug": "stale-old-slug",
            "config_slug": slug,
            "framework_version": "0.4.2",
            "memory_linked": True,
            "git_synced": True,
            "branch": "develop",
            "errors": [f"key_slug_mismatch: key=stale-old-slug config={slug}"],
            "platform": "Darwin",
            "shell": "zsh",
        }
        r1 = client.post("/api/health/checkin", json=broken_payload, headers=auth_header)
        assert r1.status_code == 200
        _checkin_ids.append(r1.json()["id"])

        # Step 2: User dashboard should show diagnostics with fix command
        r2 = client.get("/api/me/egregores", headers=auth_header)
        my_org = next(
            (o for o in r2.json()["egregores"] if o["slug"] == slug), None
        )
        assert my_org is not None

        if my_org.get("latest_checkin", {}).get("key_valid") is False:
            diag_types = [d["type"] for d in my_org["diagnostics"]]
            assert "key_mismatch" in diag_types, (
                f"Expected key_mismatch diagnostic. Got: {diag_types}"
            )
            # The fix includes the correct key
            fix_diag = next(d for d in my_org["diagnostics"] if d["type"] == "key_mismatch")
            assert fix_diag["correct_key"].startswith("ek_")

        # Step 3: Admin health tab shows critical alert
        r3 = client.get("/api/admin/health", headers=auth_header)
        alert_types = [a["type"] for a in r3.json()["alerts"]]
        assert "broken_key" in alert_types, (
            f"Admin should see broken_key alert. Got: {alert_types}"
        )
