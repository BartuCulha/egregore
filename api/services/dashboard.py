"""Personal dashboard queries — server-side.

GET /api/personal/dashboard endpoint. All personal data in one call.
Runs 7 concurrent Neo4j queries optimized for the resumption use case:
"where was I, what should I do next?"
"""

import asyncio

from .graph import execute_query
from .activity import _to_records, _to_record, _resolve_person_name


async def _my_sessions(org: dict, me: str, time_range: str) -> dict:
    """Q1: My sessions with quest links and handoff targets."""
    return await execute_query(org, """
        MATCH (s:Session)-[:BY]->(p:Person {name: $me})
        WHERE s.date >= date() - duration($timeRange)
        OPTIONAL MATCH (s)-[:INVOLVES]->(q:Quest)
        OPTIONAL MATCH (s)-[:HANDED_TO]->(target:Person)
        RETURN s.id AS id, s.date AS date, s.topic AS topic, s.branch AS branch,
               s.status AS status, s.summary AS summary, s.startedAt AS startedAt,
               s.wrappedAt AS wrappedAt, target.name AS handedTo,
               collect(DISTINCT q.id) AS quests
        ORDER BY s.date DESC, s.startedAt DESC LIMIT 20
    """, {"me": me, "timeRange": time_range})


async def _my_todos(org: dict, me: str) -> dict:
    """Q2: Open todos with quest context."""
    return await execute_query(org, """
        MATCH (t:Todo)-[:BY]->(p:Person {name: $me})
        WHERE t.status IN ['open', 'blocked', 'deferred']
        OPTIONAL MATCH (t)-[:PART_OF]->(q:Quest)
        RETURN t.id AS id, t.text AS text, t.status AS status,
               t.priority AS priority, t.blockedBy AS blockedBy,
               t.deferredUntil AS deferredUntil, q.id AS quest
        ORDER BY t.priority DESC, t.created DESC LIMIT 10
    """, {"me": me})


async def _my_quests(org: dict, me: str) -> dict:
    """Q3: Active quests where I have sessions."""
    return await execute_query(org, """
        MATCH (s:Session)-[:BY]->(p:Person {name: $me}),
              (s)-[:INVOLVES]->(q:Quest {status: 'active'})
        WITH q, count(DISTINCT s) AS mySessions, max(s.date) AS lastSession
        OPTIONAL MATCH (a:Artifact)-[:PART_OF]->(q)
        WITH q, mySessions, lastSession, count(a) AS artifacts
        RETURN q.id AS quest, q.title AS title, mySessions, artifacts,
               duration.inDays(date(left(toString(lastSession), 10)), date()).days AS daysSince
        ORDER BY lastSession DESC LIMIT 5
    """, {"me": me})


async def _handoffs_to_me(org: dict, me: str) -> dict:
    """Q4: Pending/read handoffs directed at me."""
    return await execute_query(org, """
        MATCH (s:Session)-[:HANDED_TO]->(p:Person {name: $me})
        WHERE coalesce(s.handoffStatus, 'pending') IN ['pending', 'read']
        MATCH (s)-[:BY]->(author:Person)
        RETURN s.id AS sessionId, s.topic AS topic, s.date AS date,
               author.name AS author, coalesce(s.handoffStatus, 'pending') AS status
        ORDER BY
          CASE coalesce(s.handoffStatus, 'pending') WHEN 'pending' THEN 0 ELSE 1 END,
          s.date DESC
        LIMIT 5
    """, {"me": me})


async def _open_threads(org: dict, me: str) -> dict:
    """Q5: Open threads from recent wrapped sessions.

    These are the unfinished items captured by /wrap — the strongest
    resumption signals for "where was I?"
    """
    return await execute_query(org, """
        MATCH (s:Session)-[:BY]->(p:Person {name: $me})
        WHERE s.status = 'wrapped' AND s.openThreads IS NOT NULL
              AND size(s.openThreads) > 0
        RETURN s.id AS sessionId, s.topic AS topic, s.date AS date,
               s.branch AS branch, s.openThreads AS threads
        ORDER BY s.date DESC, s.wrappedAt DESC LIMIT 3
    """, {"me": me})


async def _personal_stats(org: dict, me: str, time_range: str) -> dict:
    """Q6: Stats that answer real questions, not just count activity.

    - totalSessions: how many sessions in range
    - openTodos: unfinished work count
    - longestOpenThread: days since oldest open todo (staleness signal)
    - wrappedRatio: wrapped sessions / total sessions (closure habit)
    """
    return await execute_query(org, """
        OPTIONAL MATCH (s:Session)-[:BY]->(p:Person {name: $me})
        WHERE s.date >= date() - duration($timeRange)
        WITH count(s) AS totalSessions,
             count(CASE WHEN s.status = 'wrapped' THEN 1 END) AS wrappedSessions
        OPTIONAL MATCH (t:Todo)-[:BY]->(p2:Person {name: $me})
        WHERE t.status IN ['open', 'blocked']
        WITH totalSessions, wrappedSessions, count(t) AS openTodos,
             min(t.created) AS oldestTodo
        RETURN totalSessions, wrappedSessions, openTodos,
               CASE WHEN oldestTodo IS NOT NULL
                 THEN duration.inDays(date(left(toString(oldestTodo), 10)), date()).days
                 ELSE null END AS oldestTodoDays
    """, {"me": me, "timeRange": time_range})


async def _current_session(org: dict, session_id: str | None) -> dict:
    """Q7: Current session info (if session_id provided)."""
    if not session_id:
        return {"fields": [], "values": []}
    return await execute_query(org, """
        MATCH (s:Session {id: $sessionId})
        RETURN s.id AS id, s.status AS status, s.topic AS topic, s.branch AS branch
    """, {"sessionId": session_id})


async def get_personal_dashboard(
    org: dict,
    github_username: str,
    time_range: str = "P7D",
    session_id: str | None = None,
) -> dict:
    """Run all personal dashboard queries concurrently.

    1. Resolve Person name from github_username
    2. Run 7 queries concurrently
    3. Return structured JSON for bin/dashboard-data.sh
    """
    me = await _resolve_person_name(org, github_username)

    results = await asyncio.gather(
        _my_sessions(org, me, time_range),       # 0
        _my_todos(org, me),                       # 1
        _my_quests(org, me),                      # 2
        _handoffs_to_me(org, me),                 # 3
        _open_threads(org, me),                    # 4
        _personal_stats(org, me, time_range),     # 5
        _current_session(org, session_id),         # 6
        return_exceptions=True,
    )

    def safe(r):
        if isinstance(r, Exception):
            return {"error": str(r)}
        return r

    stats = _to_record(safe(results[5]))
    sessions = _to_records(safe(results[0]))

    # Identity mismatch detection: if stats show 0 sessions but we have
    # a session_id (auto-capture just ran), flag it so the client can
    # surface a hint rather than showing a confusing empty state.
    identity_hint = None
    total = stats.get("totalSessions") or 0
    if total == 0 and session_id:
        identity_hint = (
            "No sessions found for this identity. "
            "Your graph name may differ from your GitHub username."
        )

    return {
        "me": me,
        "time_range": time_range,
        "sessions": sessions,
        "todos": _to_records(safe(results[1])),
        "quests": _to_records(safe(results[2])),
        "handoffs": _to_records(safe(results[3])),
        "open_threads": _to_records(safe(results[4])),
        "stats": stats,
        "current_session": _to_record(safe(results[6])),
        "identity_hint": identity_hint,
    }
