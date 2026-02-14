"""Tests for the CASS transcript pipeline.

Covers:
- API endpoint (consent gate, dedup, size limit, upload flow)
- Transcript service (storage path computation, dedup check)
- Bash script security (no secrets leak, input sanitization)
"""

import gzip
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# TRANSCRIPT SERVICE UNIT TESTS
# =============================================================================


class TestComputeStoragePath:
    """Test _compute_storage_path for correct directory layout."""

    def test_with_valid_timestamp(self):
        from api.services.transcripts import _compute_storage_path
        path = _compute_storage_path("curvelabs", "abc-123", "2026-02-14T10:30:00Z")
        assert path == "transcripts/curvelabs/2026-02/abc-123.jsonl.gz"

    def test_with_timezone_offset(self):
        from api.services.transcripts import _compute_storage_path
        path = _compute_storage_path("curvelabs", "abc-123", "2026-02-14T10:30:00+02:00")
        assert path == "transcripts/curvelabs/2026-02/abc-123.jsonl.gz"

    def test_with_empty_timestamp_uses_current_month(self):
        from api.services.transcripts import _compute_storage_path
        path = _compute_storage_path("curvelabs", "abc-123", "")
        # Should use current month — just check the structure
        assert path.startswith("transcripts/curvelabs/")
        assert path.endswith("/abc-123.jsonl.gz")
        # Month part should be YYYY-MM format
        month_part = path.split("/")[2]
        assert len(month_part) == 7  # YYYY-MM

    def test_with_invalid_timestamp_falls_back(self):
        from api.services.transcripts import _compute_storage_path
        path = _compute_storage_path("curvelabs", "abc-123", "not-a-date")
        assert path.startswith("transcripts/curvelabs/")
        assert path.endswith("/abc-123.jsonl.gz")

    def test_slug_isolation(self):
        """Different orgs get different directories."""
        from api.services.transcripts import _compute_storage_path
        path_a = _compute_storage_path("org-a", "sess-1", "2026-01-01T00:00:00Z")
        path_b = _compute_storage_path("org-b", "sess-1", "2026-01-01T00:00:00Z")
        assert "org-a" in path_a
        assert "org-b" in path_b
        assert path_a != path_b


class TestStoreTranscript:
    """Test store_transcript routes correctly between CL (staging dir) and customer (Supabase)."""

    @pytest.mark.asyncio
    async def test_cl_org_writes_to_staging(self):
        """CL internal org writes to local staging directory."""
        from api.services.transcripts import store_transcript

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"TRANSCRIPT_STAGING_DIR": tmpdir}):
                org = {"slug": "curvelabs"}
                file_data = gzip.compress(b'{"test": true}\n')
                metadata = {"started_at": "2026-02-14T00:00:00Z"}

                path = await store_transcript(org, "sess-abc", file_data, metadata)

                assert path == "transcripts/curvelabs/2026-02/sess-abc.jsonl.gz"
                full_path = Path(tmpdir) / path
                assert full_path.exists()
                assert full_path.read_bytes() == file_data

    @pytest.mark.asyncio
    async def test_cl_org_creates_directories(self):
        from api.services.transcripts import store_transcript

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"TRANSCRIPT_STAGING_DIR": tmpdir}):
                org = {"slug": "curvelabs"}
                file_data = b"compressed data"
                metadata = {"started_at": "2026-06-15T12:00:00Z"}

                path = await store_transcript(org, "sess-xyz", file_data, metadata)

                assert Path(tmpdir, "transcripts", "curvelabs", "2026-06").is_dir()

    @pytest.mark.asyncio
    async def test_customer_org_uploads_to_supabase(self):
        """Customer orgs upload to Supabase Storage instead of local staging."""
        import api.services.transcripts as transcripts_mod

        mock_upload = MagicMock()
        with patch.dict("sys.modules", {"api.services.supabase": MagicMock(upload_transcript=mock_upload)}):
            org = {"slug": "acme"}
            file_data = gzip.compress(b'{"customer": true}\n')
            metadata = {"started_at": "2026-03-01T00:00:00Z"}

            path = await transcripts_mod.store_transcript(org, "sess-cust-1", file_data, metadata)

            assert path == "transcripts/acme/2026-03/sess-cust-1.jsonl.gz"
            mock_upload.assert_called_once_with("acme", path, file_data)

    @pytest.mark.asyncio
    async def test_customer_org_does_not_write_locally(self):
        """Customer org should NOT write to the staging directory."""
        import api.services.transcripts as transcripts_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"TRANSCRIPT_STAGING_DIR": tmpdir}), \
                 patch.dict("sys.modules", {"api.services.supabase": MagicMock(upload_transcript=MagicMock())}):
                org = {"slug": "acme"}
                file_data = b"compressed"
                metadata = {"started_at": "2026-03-01T00:00:00Z"}

                await transcripts_mod.store_transcript(org, "sess-cust-2", file_data, metadata)

                # Nothing should be written to the staging dir
                assert not list(Path(tmpdir).rglob("*.gz"))


# =============================================================================
# API ENDPOINT TESTS
# =============================================================================


class TestTranscriptUploadEndpoint:
    """Test /api/transcript/upload via FastAPI test client."""

    @pytest.mark.asyncio
    async def test_rejects_without_consent(self):
        """403 if org has transcript_sharing disabled."""
        from api.main import app
        from api.auth import validate_api_key as real_validate
        from httpx import AsyncClient, ASGITransport

        org_no_consent = {"slug": "testorg", "transcript_sharing": False}

        async def mock_validate():
            return org_no_consent

        app.dependency_overrides[real_validate] = mock_validate
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/transcript/upload",
                    headers={"Authorization": "Bearer ek_test_fake123456789"},
                    files={"file": ("test.jsonl.gz", b"data", "application/gzip")},
                    data={"session_id": "sess-1"},
                )
                assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_duplicate_for_existing(self):
        """Returns duplicate status if session already indexed."""
        from api.main import app
        from api.auth import validate_api_key as real_validate
        from httpx import AsyncClient, ASGITransport

        org = {"slug": "testorg", "transcript_sharing": True}

        async def mock_validate():
            return org

        app.dependency_overrides[real_validate] = mock_validate
        try:
            with patch("api.services.transcripts.check_duplicate", new_callable=AsyncMock, return_value=True):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(
                        "/api/transcript/upload",
                        headers={"Authorization": "Bearer ek_test_fake123456789"},
                        files={"file": ("test.jsonl.gz", b"data", "application/gzip")},
                        data={"session_id": "sess-existing"},
                    )
                    assert resp.status_code == 200
                    assert resp.json()["status"] == "duplicate"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_rejects_oversized_file(self):
        """413 if file exceeds 50MB."""
        from api.main import app
        from api.auth import validate_api_key as real_validate
        from httpx import AsyncClient, ASGITransport

        org = {"slug": "testorg", "transcript_sharing": True}
        big_data = b"x" * (51 * 1024 * 1024)  # 51MB

        async def mock_validate():
            return org

        app.dependency_overrides[real_validate] = mock_validate
        try:
            with patch("api.services.transcripts.check_duplicate", new_callable=AsyncMock, return_value=False):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(
                        "/api/transcript/upload",
                        headers={"Authorization": "Bearer ek_test_fake123456789"},
                        files={"file": ("big.jsonl.gz", big_data, "application/gzip")},
                        data={"session_id": "sess-big"},
                    )
                    assert resp.status_code == 413
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_successful_upload(self):
        """Happy path: consent + new session + valid size."""
        from api.main import app
        from api.auth import validate_api_key as real_validate
        from httpx import AsyncClient, ASGITransport

        org = {"slug": "testorg", "transcript_sharing": True}
        file_data = gzip.compress(b'{"message": "hello"}\n')

        async def mock_validate():
            return org

        app.dependency_overrides[real_validate] = mock_validate
        try:
            with patch("api.services.transcripts.check_duplicate", new_callable=AsyncMock, return_value=False), \
                 patch("api.services.transcripts.store_transcript", new_callable=AsyncMock, return_value="transcripts/testorg/2026-02/sess-new.jsonl.gz"), \
                 patch("api.services.transcripts.index_transcript", new_callable=AsyncMock, return_value={}):
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    resp = await client.post(
                        "/api/transcript/upload",
                        headers={"Authorization": "Bearer ek_test_fake123456789"},
                        files={"file": ("test.jsonl.gz", file_data, "application/gzip")},
                        data={
                            "session_id": "sess-new",
                            "author": "ozzibroccoli",
                            "branch": "dev/oz/test",
                            "message_count": "42",
                        },
                    )
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["status"] == "uploaded"
                    assert body["session_id"] == "sess-new"
        finally:
            app.dependency_overrides.clear()


# =============================================================================
# BASH SCRIPT SECURITY TESTS
# =============================================================================


class TestArchiveScriptSecurity:
    """Verify transcript-archive.sh doesn't leak sensitive info."""

    SCRIPT = str(Path(__file__).parent.parent / "bin" / "transcript-archive.sh")

    def test_script_exists_and_executable(self):
        assert os.path.isfile(self.SCRIPT)
        assert os.access(self.SCRIPT, os.X_OK)

    def test_output_fully_suppressed(self):
        """Script should produce NO stdout/stderr output, even on error."""
        result = subprocess.run(
            ["bash", self.SCRIPT],
            input=b'{}',  # empty JSON → should exit silently
            capture_output=True,
            timeout=5,
        )
        assert result.stdout == b"", f"Unexpected stdout: {result.stdout[:200]}"
        assert result.stderr == b"", f"Unexpected stderr: {result.stderr[:200]}"

    def test_no_output_with_invalid_json(self):
        """Malformed input should not produce error messages."""
        result = subprocess.run(
            ["bash", self.SCRIPT],
            input=b'not json at all',
            capture_output=True,
            timeout=5,
        )
        assert result.stdout == b""
        assert result.stderr == b""

    def test_no_output_with_path_traversal(self):
        """Path traversal in session_id should be silent."""
        malicious = json.dumps({
            "session_id": "../../etc/passwd",
            "transcript_path": "/etc/passwd",
        }).encode()
        result = subprocess.run(
            ["bash", self.SCRIPT],
            input=malicious,
            capture_output=True,
            timeout=5,
        )
        assert result.stdout == b""
        assert result.stderr == b""

    def test_no_secrets_in_script_source(self):
        """Script source should not contain hardcoded secrets."""
        content = Path(self.SCRIPT).read_text()
        # Should not contain actual tokens or passwords
        assert "sk-" not in content
        assert "ek_" not in content
        assert "password" not in content.lower().replace("neo4j_password", "")
        # API key is read from .env at runtime, not hardcoded
        assert "EGREGORE_API_KEY=" not in content.split("grep")[0] if "grep" in content else True

    def test_rejects_path_outside_claude_dir(self):
        """transcript_path outside ~/.claude/ should be rejected silently."""
        payload = json.dumps({
            "session_id": "test-session-123",
            "transcript_path": "/etc/passwd",
        }).encode()
        result = subprocess.run(
            ["bash", self.SCRIPT],
            input=payload,
            capture_output=True,
            timeout=5,
        )
        # Should exit cleanly without processing the file
        assert result.returncode == 0
        assert result.stdout == b""

    def test_session_id_sanitized(self):
        """Special characters in session_id should be stripped."""
        # This is tested indirectly — the script uses tr -cd 'a-zA-Z0-9_-'
        content = Path(self.SCRIPT).read_text()
        assert "tr -cd" in content, "Script should sanitize session_id with tr -cd"

    def test_exec_devnull_present(self):
        """exec >/dev/null 2>&1 must be near the top."""
        content = Path(self.SCRIPT).read_text()
        lines = content.split("\n")
        # Should appear within first 10 lines
        devnull_lines = [i for i, l in enumerate(lines) if "exec >/dev/null 2>&1" in l]
        assert devnull_lines, "Script must suppress all output with exec >/dev/null"
        assert devnull_lines[0] <= 12, "exec >/dev/null should be near the top of the script"


class TestBackfillScriptSecurity:
    """Verify transcript-backfill.sh doesn't leak sensitive info."""

    SCRIPT = str(Path(__file__).parent.parent / "bin" / "transcript-backfill.sh")

    def test_script_exists_and_executable(self):
        assert os.path.isfile(self.SCRIPT)
        assert os.access(self.SCRIPT, os.X_OK)

    def test_no_secrets_in_script_source(self):
        """No hardcoded secrets."""
        content = Path(self.SCRIPT).read_text()
        assert "sk-" not in content
        assert "ek_" not in content

    def test_no_token_echo(self):
        """Script should never echo/print tokens or API keys."""
        content = Path(self.SCRIPT).read_text()
        # Should not echo any variable that could contain secrets
        assert "echo $API_KEY" not in content
        assert "echo $GH_TOKEN" not in content
        assert "echo $GITHUB_TOKEN" not in content

    def test_author_sanitized(self):
        """Author field should be sanitized."""
        content = Path(self.SCRIPT).read_text()
        assert "tr -cd" in content, "Script should sanitize AUTHOR with tr -cd"


# =============================================================================
# MODEL TESTS
# =============================================================================


class TestOrgSetupModel:
    """Verify transcript_sharing field on OrgSetup."""

    def test_defaults_to_false(self):
        from api.models import OrgSetup
        setup = OrgSetup(github_org="test", org_name="Test Org")
        assert setup.transcript_sharing is False

    def test_can_be_enabled(self):
        from api.models import OrgSetup
        setup = OrgSetup(github_org="test", org_name="Test Org", transcript_sharing=True)
        assert setup.transcript_sharing is True


# =============================================================================
# SETTINGS HOOK REGISTRATION
# =============================================================================


class TestHookRegistration:
    """Verify SessionEnd hook is registered in settings."""

    def test_session_end_hook_registered(self):
        settings_path = Path(__file__).parent.parent / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        hooks = settings.get("hooks", {})
        assert "SessionEnd" in hooks, "SessionEnd hook must be registered"
        session_end = hooks["SessionEnd"]
        assert len(session_end) > 0
        command = session_end[0]["hooks"][0]["command"]
        assert "transcript-archive.sh" in command


class TestSyncignore:
    """Verify syncignore rules for transcript scripts."""

    def test_archive_is_public(self):
        """Archive script syncs to public repo (all orgs need it)."""
        syncignore_path = Path(__file__).parent.parent / ".syncignore"
        content = syncignore_path.read_text()
        assert "bin/transcript-archive.sh" not in content

    def test_backfill_is_private(self):
        """Backfill script stays CL-only."""
        syncignore_path = Path(__file__).parent.parent / ".syncignore"
        content = syncignore_path.read_text()
        assert "bin/transcript-backfill.sh" in content
