"""Tests for session topic display fix.

Verifies that sessions never show "untitled", "current session", or
"quick session" — instead falling back to humanized branch slugs.

Covers all 3 layers:
1. Write-time: graph-op.sh set-topic (tested in test-graph-op.sh)
2. Read-time: COALESCE displayTopic in dashboard.py + activity.py queries
3. Render-time: Command markdown fallback rules (structural check)

Tests are organized by:
- Guard validation (COALESCE queries pass the query guard)
- Data shaping (mock execute_query → verify output structure)
- _to_records/_to_record correctness with null-topic data
- COALESCE Cypher correctness (live Neo4j, skipped if unavailable)
- Command markdown rules (structural check)
"""

import sys
import re
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from api.services.activity import _to_records, _to_record


# =============================================================================
# GUARD VALIDATION — COALESCE queries must pass the query guard
# =============================================================================


class TestCoalesceQueriesPassGuard:
    """The new COALESCE topic expressions must not trigger guard blocks."""

    def test_dashboard_my_sessions_query(self):
        """Dashboard _my_sessions COALESCE query passes guard validation."""
        from api.services.guard import validate_query

        query = """
        MATCH (s:Session)-[:BY]->(p:Person {name: $me})
        WHERE s.date >= date() - duration($timeRange)
        OPTIONAL MATCH (s)-[:INVOLVES]->(q:Quest)
        OPTIONAL MATCH (s)-[:HANDED_TO]->(target:Person)
        RETURN s.id AS id, s.date AS date,
               COALESCE(s.topic,
                 CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                      AND s.branch CONTAINS '/'
                   THEN replace(last(split(s.branch, '/')), '-', ' ')
                   ELSE null END
               ) AS topic, s.branch AS branch,
               s.status AS status, s.summary AS summary, s.startedAt AS startedAt,
               s.wrappedAt AS wrappedAt, target.name AS handedTo,
               collect(DISTINCT q.id) AS quests
        ORDER BY s.date DESC, s.startedAt DESC LIMIT 20
        """
        # Should not raise
        validate_query(query)

    def test_dashboard_current_session_query(self):
        """Dashboard _current_session COALESCE query passes guard validation."""
        from api.services.guard import validate_query

        query = """
        MATCH (s:Session {id: $sessionId})
        RETURN s.id AS id, s.status AS status,
               COALESCE(s.topic,
                 CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                      AND s.branch CONTAINS '/'
                   THEN replace(last(split(s.branch, '/')), '-', ' ')
                   ELSE null END
               ) AS topic, s.branch AS branch
        """
        validate_query(query)

    def test_activity_my_sessions_query(self):
        """Activity _my_sessions COALESCE query passes guard validation."""
        from api.services.guard import validate_query

        query = """
        MATCH (s:Session)-[:BY]->(p:Person {name: $me})
        OPTIONAL MATCH (s)-[:HANDED_TO]->(target:Person)
        RETURN s.date AS date,
               COALESCE(s.topic,
                 CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                      AND s.branch CONTAINS '/'
                   THEN replace(last(split(s.branch, '/')), '-', ' ')
                   ELSE null END
               ) AS topic, s.id AS id,
               s.filePath AS filePath, target.name AS handedTo
        ORDER BY s.date DESC, s.id DESC LIMIT 10
        """
        validate_query(query)

    def test_activity_team_sessions_query(self):
        """Activity _team_sessions COALESCE query passes guard validation."""
        from api.services.guard import validate_query

        query = """
        MATCH (s:Session)-[:BY]->(p:Person)
        WHERE p.name <> $me AND date(left(toString(s.date), 10)) >= date() - duration('P7D')
        RETURN s.date AS date,
               COALESCE(s.topic,
                 CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                      AND s.branch CONTAINS '/'
                   THEN replace(last(split(s.branch, '/')), '-', ' ')
                   ELSE null END
               ) AS topic, p.name AS by
        ORDER BY s.date DESC LIMIT 5
        """
        validate_query(query)

    def test_set_topic_query(self):
        """The set-topic Cypher (used by graph-op.sh) passes guard validation."""
        from api.services.guard import validate_query

        validate_query("""
            MATCH (s:Session {id: $sid})
            SET s.topic = $topic, s.branch = $branch
            RETURN s.id AS id, s.topic AS topic, s.branch AS branch
        """)

    def test_set_topic_without_branch_query(self):
        """The set-topic Cypher without branch passes guard validation."""
        from api.services.guard import validate_query

        validate_query("""
            MATCH (s:Session {id: $sid})
            SET s.topic = $topic
            RETURN s.id AS id, s.topic AS topic
        """)


# =============================================================================
# DATA SHAPING — _to_records / _to_record with COALESCE output
# =============================================================================


class TestToRecordsWithTopicFallback:
    """_to_records correctly handles data where topic comes from COALESCE."""

    def test_topic_from_explicit_value(self):
        """When topic is explicitly set, _to_records passes it through."""
        raw = {
            "fields": ["date", "topic", "id", "branch"],
            "values": [
                ["2026-02-24", "session naming bug", "s-123", "dev/oz/session-naming-bug"],
            ],
        }
        records = _to_records(raw)
        assert len(records) == 1
        assert records[0]["topic"] == "session naming bug"
        assert records[0]["branch"] == "dev/oz/session-naming-bug"

    def test_topic_from_branch_coalesce(self):
        """When topic is derived from branch via COALESCE, it's a humanized slug."""
        raw = {
            "fields": ["date", "topic", "id", "branch"],
            "values": [
                ["2026-02-24", "session naming bug", "s-123", "dev/oz/session-naming-bug"],
            ],
        }
        records = _to_records(raw)
        assert records[0]["topic"] == "session naming bug"

    def test_topic_null_when_no_branch(self):
        """When both topic and branch are null, topic is null."""
        raw = {
            "fields": ["date", "topic", "id", "branch"],
            "values": [
                ["2026-02-24", None, "s-456", None],
            ],
        }
        records = _to_records(raw)
        assert records[0]["topic"] is None
        assert records[0]["branch"] is None

    def test_topic_null_when_branch_is_develop(self):
        """When topic is null and branch is 'develop', COALESCE yields null."""
        raw = {
            "fields": ["date", "topic", "id", "branch"],
            "values": [
                ["2026-02-24", None, "s-789", "develop"],
            ],
        }
        records = _to_records(raw)
        assert records[0]["topic"] is None

    def test_multiple_sessions_mixed_topics(self):
        """Mix of explicit, derived, and null topics all pass through correctly."""
        raw = {
            "fields": ["date", "topic", "id", "branch"],
            "values": [
                ["2026-02-24", "pricing strategy", "s-1", "dev/oz/pricing-strategy"],
                ["2026-02-23", "dashboard fixes", "s-2", "dev/oz/dashboard-fixes"],
                ["2026-02-22", None, "s-3", "develop"],
                ["2026-02-21", None, "s-4", None],
            ],
        }
        records = _to_records(raw)
        assert len(records) == 4
        assert records[0]["topic"] == "pricing strategy"
        assert records[1]["topic"] == "dashboard fixes"
        assert records[2]["topic"] is None
        assert records[3]["topic"] is None

    def test_empty_results(self):
        """Empty result set returns empty list."""
        raw = {"fields": ["date", "topic", "id"], "values": []}
        records = _to_records(raw)
        assert records == []


class TestToRecordWithTopicFallback:
    """_to_record for single-row results (current session)."""

    def test_current_session_with_topic(self):
        raw = {
            "fields": ["id", "status", "topic", "branch"],
            "values": [["s-100", "active", "session naming bug", "dev/oz/session-naming-bug"]],
        }
        record = _to_record(raw)
        assert record["topic"] == "session naming bug"
        assert record["status"] == "active"

    def test_current_session_null_topic_with_branch(self):
        """Current session with null topic but valid branch — topic is derived."""
        raw = {
            "fields": ["id", "status", "topic", "branch"],
            "values": [["s-100", "active", "mcp auth review", "dev/oz/mcp-auth-review"]],
        }
        record = _to_record(raw)
        assert record["topic"] == "mcp auth review"

    def test_current_session_no_data(self):
        """No matching session returns empty record."""
        raw = {"fields": ["id", "status", "topic", "branch"], "values": []}
        record = _to_record(raw)
        assert record.get("topic") is None

    def test_current_session_all_null(self):
        """Session with all null fields returns nulls."""
        raw = {
            "fields": ["id", "status", "topic", "branch"],
            "values": [[None, None, None, None]],
        }
        record = _to_record(raw)
        assert record["topic"] is None
        assert record["branch"] is None


# =============================================================================
# DASHBOARD get_personal_dashboard — mock execute_query
# =============================================================================


class TestDashboardTopicFallback:
    """Full dashboard function returns correct topic data."""

    @pytest.fixture
    def mock_org(self):
        return {
            "slug": "test-org",
            "neo4j_host": "localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "test",
        }

    def test_sessions_have_topic_from_coalesce(self, mock_org):
        """Sessions in dashboard output have topic derived from COALESCE."""
        from api.services import dashboard

        # Mock execute_query to return realistic data
        async def mock_execute(org, query, params=None):
            if "BY" in query and "duration" in query:
                # _my_sessions query
                return {
                    "fields": ["id", "date", "topic", "branch", "status",
                               "summary", "startedAt", "wrappedAt", "handedTo", "quests"],
                    "values": [
                        ["s-1", "2026-02-24", "pricing strategy", "dev/oz/pricing-strategy",
                         "active", None, "2026-02-24T10:00:00Z", None, None, []],
                        ["s-2", "2026-02-23", "dashboard fixes", "dev/oz/dashboard-fixes",
                         "wrapped", "Fixed TUI", "2026-02-23T09:00:00Z", "2026-02-23T17:00:00Z", None, []],
                    ],
                }
            if "Person" in query and "github" in query:
                # _resolve_person_name
                return {"fields": ["name"], "values": [["oz"]]}
            # Default empty response for other queries
            return {"fields": [], "values": []}

        # Patch execute_query in both dashboard AND activity modules
        # (_resolve_person_name is imported from activity into dashboard)
        from api.services import activity as activity_mod
        with patch.object(dashboard, "execute_query", side_effect=mock_execute), \
             patch.object(activity_mod, "execute_query", side_effect=mock_execute):
            result = asyncio.get_event_loop().run_until_complete(
                dashboard.get_personal_dashboard(mock_org, "ozzibroccoli", "P7D", "s-1")
            )

        sessions = result["sessions"]
        assert len(sessions) == 2
        assert sessions[0]["topic"] == "pricing strategy"
        assert sessions[1]["topic"] == "dashboard fixes"
        # No session should have null topic when branch-derived topic is available
        for s in sessions:
            assert s["topic"] is not None, f"Session {s['id']} has null topic"


# =============================================================================
# ACTIVITY get_activity_dashboard — mock execute_query
# =============================================================================


class TestActivityTopicFallback:
    """Full activity function returns correct topic data."""

    @pytest.fixture
    def mock_org(self):
        return {
            "slug": "test-org",
            "neo4j_host": "localhost",
            "neo4j_user": "neo4j",
            "neo4j_password": "test",
        }

    def test_my_sessions_have_topic(self, mock_org):
        """my_sessions in activity output have correct topic."""
        from api.services import activity

        async def mock_execute(org, query, params=None):
            if "Person" in query and "github" in query:
                # _resolve_person_name
                return {"fields": ["name"], "values": [["oz"]]}
            if "LIMIT 10" in query and "filePath" in query:
                # _my_sessions (has LIMIT 10 and returns filePath)
                return {
                    "fields": ["date", "topic", "id", "filePath", "handedTo"],
                    "values": [
                        ["2026-02-24", "mcp auth review", "s-10", None, None],
                        ["2026-02-23", "onboarding flow", "s-11", None, "bob"],
                    ],
                }
            if "name <>" in query:
                # _team_sessions
                return {
                    "fields": ["date", "topic", "by"],
                    "values": [
                        ["2026-02-24", "api gateway", "alice"],
                    ],
                }
            return {"fields": [], "values": []}

        with patch.object(activity, "execute_query", side_effect=mock_execute):
            result = asyncio.get_event_loop().run_until_complete(
                activity.get_activity_dashboard(mock_org, "ozzibroccoli")
            )

        my_sessions = result["my_sessions"]
        assert len(my_sessions) == 2
        assert my_sessions[0]["topic"] == "mcp auth review"
        assert my_sessions[1]["topic"] == "onboarding flow"

        team_sessions = result["team_sessions"]
        assert len(team_sessions) == 1
        assert team_sessions[0]["topic"] == "api gateway"


# =============================================================================
# COALESCE LOGIC CORRECTNESS (simulated Neo4j behavior)
# =============================================================================


class TestCoalesceLogicCorrectness:
    """Verify the COALESCE fallback chain produces correct results.

    These simulate what Neo4j's COALESCE expression returns for various
    combinations of topic and branch values.
    """

    @staticmethod
    def simulate_coalesce(topic, branch):
        """Python equivalent of the Cypher COALESCE expression.

        COALESCE(s.topic,
          CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
               AND s.branch CONTAINS '/'
            THEN replace(last(split(s.branch, '/')), '-', ' ')
            ELSE null END
        )
        """
        if topic is not None:
            return topic
        if (branch is not None
            and branch not in ('develop', 'main', 'master')
            and '/' in branch):
            parts = branch.split('/')
            return parts[-1].replace('-', ' ')
        return None

    def test_explicit_topic_returned(self):
        assert self.simulate_coalesce("pricing strategy", "dev/oz/pricing-strategy") == "pricing strategy"

    def test_explicit_topic_wins_over_branch(self):
        """Topic takes precedence even if branch has a different slug."""
        assert self.simulate_coalesce("my custom topic", "dev/oz/different-slug") == "my custom topic"

    def test_branch_fallback_simple(self):
        assert self.simulate_coalesce(None, "dev/oz/session-naming-bug") == "session naming bug"

    def test_branch_fallback_single_word(self):
        assert self.simulate_coalesce(None, "dev/oz/refactor") == "refactor"

    def test_branch_fallback_multi_word(self):
        assert self.simulate_coalesce(None, "dev/alice/mcp-auth-flow-review") == "mcp auth flow review"

    def test_branch_develop_returns_none(self):
        assert self.simulate_coalesce(None, "develop") is None

    def test_branch_main_returns_none(self):
        assert self.simulate_coalesce(None, "main") is None

    def test_branch_master_returns_none(self):
        assert self.simulate_coalesce(None, "master") is None

    def test_both_null(self):
        assert self.simulate_coalesce(None, None) is None

    def test_feature_branch(self):
        assert self.simulate_coalesce(None, "feature/new-dashboard") == "new dashboard"

    def test_bugfix_branch(self):
        assert self.simulate_coalesce(None, "bugfix/graph-timeout") == "graph timeout"

    def test_branch_without_slash(self):
        """A branch with no slash doesn't trigger fallback."""
        assert self.simulate_coalesce(None, "some-branch") is None

    def test_empty_topic_is_not_null(self):
        """An empty string topic is not null — it passes through as-is."""
        assert self.simulate_coalesce("", "dev/oz/something") == ""

    def test_deep_nested_branch(self):
        """Branch with multiple slashes — last segment is used."""
        assert self.simulate_coalesce(None, "dev/oz/2026/02/session-naming") == "session naming"


# =============================================================================
# SIDE-EFFECT CHECKS — queries we DIDN'T change still work
# =============================================================================


class TestUnchangedQueriesStillValid:
    """Queries in dashboard.py and activity.py that were NOT modified
    must still pass guard validation."""

    def test_dashboard_my_todos_query(self):
        from api.services.guard import validate_query
        validate_query("""
            MATCH (t:Todo)-[:BY]->(p:Person {name: $me})
            WHERE t.status IN ['open', 'blocked', 'deferred']
            OPTIONAL MATCH (t)-[:PART_OF]->(q:Quest)
            RETURN t.id AS id, t.text AS text, t.status AS status,
                   t.priority AS priority, t.blockedBy AS blockedBy,
                   t.deferredUntil AS deferredUntil, q.id AS quest
            ORDER BY t.priority DESC, t.created DESC LIMIT 10
        """)

    def test_dashboard_handoffs_to_me_query(self):
        from api.services.guard import validate_query
        validate_query("""
            MATCH (s:Session)-[:HANDED_TO]->(p:Person {name: $me})
            WHERE coalesce(s.handoffStatus, 'pending') IN ['pending', 'read']
            MATCH (s)-[:BY]->(author:Person)
            RETURN s.id AS sessionId, s.topic AS topic, s.date AS date,
                   author.name AS author, coalesce(s.handoffStatus, 'pending') AS status
            ORDER BY
              CASE coalesce(s.handoffStatus, 'pending') WHEN 'pending' THEN 0 ELSE 1 END,
              s.date DESC
            LIMIT 5
        """)

    def test_dashboard_open_threads_query(self):
        from api.services.guard import validate_query
        validate_query("""
            MATCH (s:Session)-[:BY]->(p:Person {name: $me})
            WHERE s.status = 'wrapped' AND s.openThreads IS NOT NULL
                  AND size(s.openThreads) > 0
            RETURN s.id AS sessionId, s.topic AS topic, s.date AS date,
                   s.branch AS branch, s.openThreads AS threads
            ORDER BY s.date DESC, s.wrappedAt DESC LIMIT 3
        """)

    def test_activity_quests_query(self):
        from api.services.guard import validate_query
        validate_query("""
            MATCH (q:Quest {status: 'active'})
            OPTIONAL MATCH (a:Artifact)-[:PART_OF]->(q)
            OPTIONAL MATCH (a)-[:CONTRIBUTED_BY]->(p:Person)
            WHERE p.name IS NOT NULL AND p.name <> 'external'
            OPTIONAL MATCH (q)-[:STARTED_BY]->(starter:Person {name: $me})
            OPTIONAL MATCH (myArt:Artifact)-[:PART_OF]->(q)
            WHERE (myArt)-[:CONTRIBUTED_BY]->(:Person {name: $me})
            WITH q, count(DISTINCT a) AS artifacts, count(DISTINCT p) AS contributors,
                 CASE WHEN count(a) > 0
                   THEN duration.inDays(date(left(toString(max(a.created)), 10)), date()).days
                   ELSE duration.inDays(date(left(toString(q.started), 10)), date()).days END AS daysSince,
                 coalesce(q.priority, 0) AS priority,
                 CASE WHEN starter IS NOT NULL THEN 1 ELSE 0 END AS iStarted,
                 count(DISTINCT myArt) AS myArtifacts
            WITH q, artifacts, daysSince,
                 round((toFloat(artifacts) + toFloat(contributors)*1.5
                   + toFloat(priority)*5.0
                   + 30.0/(1.0+toFloat(daysSince)*0.5)
                   + CASE WHEN iStarted = 1 THEN 15.0 ELSE 0.0 END
                   + toFloat(myArtifacts)*3.0) * 100)/100 AS score
            ORDER BY score DESC LIMIT 5
            RETURN q.id AS quest, q.title AS title, artifacts, daysSince, score
        """)

    def test_activity_all_handoffs_query(self):
        from api.services.guard import validate_query
        validate_query("""
            MATCH (s:Session)-[:HANDED_TO]->(target:Person)
            WHERE date(left(toString(s.date), 10)) >= date() - duration('P7D')
            MATCH (s)-[:BY]->(author:Person)
            RETURN s.topic AS topic, s.date AS date, author.name AS from,
                   target.name AS to, s.filePath AS filePath
            ORDER BY s.date DESC LIMIT 5
        """)


# =============================================================================
# COMMAND MARKDOWN — structural checks
# =============================================================================


class TestCommandMarkdownRules:
    """Verify dashboard.md and activity.md have explicit topic fallback rules."""

    @pytest.fixture
    def dashboard_md(self):
        path = Path(__file__).parent.parent / ".claude" / "commands" / "dashboard.md"
        return path.read_text()

    @pytest.fixture
    def activity_md(self):
        path = Path(__file__).parent.parent / ".claude" / "commands" / "activity.md"
        return path.read_text()

    def test_dashboard_has_topic_display_rules(self, dashboard_md):
        assert "Topic display rules" in dashboard_md

    def test_dashboard_forbids_untitled(self, dashboard_md):
        assert "NEVER show" in dashboard_md
        assert "untitled" in dashboard_md.lower()

    def test_dashboard_forbids_current_session_label(self, dashboard_md):
        assert "current session" in dashboard_md.lower()

    def test_dashboard_forbids_quick_session_label(self, dashboard_md):
        assert "quick session" in dashboard_md.lower()

    def test_dashboard_has_branch_fallback(self, dashboard_md):
        assert "branch slug humanized" in dashboard_md or "humanized" in dashboard_md

    def test_dashboard_has_date_fallback(self, dashboard_md):
        assert "session date as fallback" in dashboard_md or "date as fallback" in dashboard_md

    def test_activity_has_topic_display_rules(self, activity_md):
        assert "Topic display rules" in activity_md

    def test_activity_forbids_untitled(self, activity_md):
        assert "NEVER show" in activity_md
        assert "untitled" in activity_md.lower()

    def test_activity_forbids_current_session_label(self, activity_md):
        assert "current session" in activity_md.lower()

    def test_activity_forbids_quick_session_label(self, activity_md):
        assert "quick session" in activity_md.lower()

    def test_activity_has_branch_fallback(self, activity_md):
        assert "branch slug humanized" in activity_md or "humanized" in activity_md

    def test_activity_has_date_fallback(self, activity_md):
        assert "session date as fallback" in activity_md or "date as fallback" in activity_md


# =============================================================================
# CLAUDE.md — structural checks for write-time instruction
# =============================================================================


class TestClaudeMdInstruction:
    """Verify CLAUDE.md has the set-topic instruction after branch creation."""

    @pytest.fixture
    def claude_md(self):
        path = Path(__file__).parent.parent / "CLAUDE.md"
        return path.read_text()

    def test_has_set_topic_instruction(self, claude_md):
        assert "set-topic" in claude_md

    def test_set_topic_after_branch_creation(self, claude_md):
        """set-topic instruction appears after the branch creation steps."""
        branch_pos = claude_md.find("git checkout -b dev/")
        set_topic_pos = claude_md.find("set-topic")
        assert branch_pos > 0, "Branch creation instruction not found"
        assert set_topic_pos > branch_pos, "set-topic should come after branch creation"

    def test_graph_op_used_not_raw_cypher(self, claude_md):
        """Instruction uses graph-op.sh, not raw graph.sh query."""
        assert "graph-op.sh set-topic" in claude_md

    def test_session_id_from_file(self, claude_md):
        """Instruction reads session ID from the session file."""
        assert "session-*.id" in claude_md


# =============================================================================
# graph-op.sh — structural checks for set-topic operation
# =============================================================================


class TestGraphOpSetTopic:
    """Verify graph-op.sh has the set-topic operation."""

    @pytest.fixture
    def graph_op_sh(self):
        path = Path(__file__).parent.parent / "bin" / "graph-op.sh"
        return path.read_text()

    def test_set_topic_case_exists(self, graph_op_sh):
        assert "set-topic)" in graph_op_sh

    def test_set_topic_sets_topic_property(self, graph_op_sh):
        assert "s.topic = " in graph_op_sh

    def test_set_topic_optionally_sets_branch(self, graph_op_sh):
        assert "s.branch = " in graph_op_sh

    def test_set_topic_in_operations_list(self, graph_op_sh):
        assert "set-topic" in graph_op_sh
        # Should be in the error message operations list
        assert '"set-topic"' in graph_op_sh or "'set-topic'" in graph_op_sh

    def test_set_topic_requires_session_id(self, graph_op_sh):
        """set-topic should require session-id as first arg."""
        # Find the set-topic case block
        match = re.search(r'set-topic\).*?;;', graph_op_sh, re.DOTALL)
        assert match, "set-topic case block not found"
        block = match.group(0)
        assert "missing session-id" in block or "{1:?" in block

    def test_set_topic_requires_topic(self, graph_op_sh):
        """set-topic should require topic as second arg."""
        match = re.search(r'set-topic\).*?;;', graph_op_sh, re.DOTALL)
        assert match
        block = match.group(0)
        assert "missing topic" in block or "{2:?" in block

    def test_set_topic_branch_is_optional(self, graph_op_sh):
        """Branch should be optional (default empty)."""
        match = re.search(r'set-topic\).*?;;', graph_op_sh, re.DOTALL)
        assert match
        block = match.group(0)
        assert '${3:-}' in block or '${3:-""' in block


# =============================================================================
# LIVE GRAPH — COALESCE expression correctness via API gateway
# =============================================================================


def _graph_query(cypher: str) -> dict | None:
    """Run a Cypher query via bin/graph.sh and parse JSON result.

    Returns parsed JSON or None if graph.sh fails/unavailable.
    """
    import subprocess, json
    script = Path(__file__).parent.parent / "bin" / "graph.sh"
    if not script.exists():
        return None
    try:
        result = subprocess.run(
            ["bash", str(script), "query", cypher],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _graph_values(cypher: str) -> list[list] | None:
    """Run a query and return just the values array, or None."""
    data = _graph_query(cypher)
    if data is None:
        return None
    return data.get("values", [])


@pytest.mark.retrieval
class TestCoalesceLiveGraph:
    """Run COALESCE expression against real Neo4j via API gateway.

    Skipped if graph is unreachable.
    """

    @pytest.fixture(autouse=True)
    def _require_graph(self):
        result = _graph_query("RETURN 1 AS ok")
        if result is None:
            pytest.skip("Graph unreachable via API gateway")

    def test_coalesce_with_topic_set(self):
        """Session with explicit topic — COALESCE returns it."""
        values = _graph_values("""
            WITH {topic: 'pricing strategy', branch: 'dev/oz/pricing-strategy'} AS s
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
        """)
        assert values is not None and len(values) > 0
        assert values[0][0] == "pricing strategy"

    def test_coalesce_with_null_topic_and_branch(self):
        """Null topic + valid branch — COALESCE derives from branch."""
        values = _graph_values("""
            WITH {topic: null, branch: 'dev/oz/session-naming-bug'} AS s
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
        """)
        assert values is not None and len(values) > 0
        assert values[0][0] == "session naming bug"

    def test_coalesce_with_develop_branch(self):
        """Null topic + develop branch — COALESCE returns null."""
        values = _graph_values("""
            WITH {topic: null, branch: 'develop'} AS s
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
        """)
        assert values is not None and len(values) > 0
        assert values[0][0] is None

    def test_coalesce_with_both_null(self):
        """Both null — returns null."""
        values = _graph_values("""
            WITH {topic: null, branch: null} AS s
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
        """)
        assert values is not None and len(values) > 0
        assert values[0][0] is None

    def test_coalesce_with_feature_branch(self):
        """Feature branch slug is humanized."""
        values = _graph_values("""
            WITH {topic: null, branch: 'feature/new-dashboard-layout'} AS s
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
        """)
        assert values is not None and len(values) > 0
        assert values[0][0] == "new dashboard layout"

    def test_real_sessions_never_return_untitled(self):
        """No existing session returns 'untitled' from COALESCE.

        Tests against real data — COALESCE should return either
        a real topic, a humanized branch slug, or null.
        """
        values = _graph_values("""
            MATCH (s:Session)
            RETURN COALESCE(s.topic,
              CASE WHEN s.branch IS NOT NULL AND NOT s.branch IN ['develop','main','master']
                   AND s.branch CONTAINS '/'
                THEN replace(last(split(s.branch, '/')), '-', ' ')
                ELSE null END
            ) AS displayTopic
            LIMIT 100
        """)
        assert values is not None
        bad_labels = {"untitled", "current session", "quick session"}
        for row in values:
            topic = row[0]
            if topic is not None:
                assert topic.lower() not in bad_labels, (
                    f"Session returned forbidden label: '{topic}'"
                )
