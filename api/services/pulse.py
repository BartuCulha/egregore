"""Pulse spirit — post-session synthesis using Sonnet 4.6.

Receives structured session data (transcript, observation summary, graph context)
and returns synthesis: graph edges to create, cross-person signals, and a brief
for the person's next session greeting.
"""

import json
import os
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — required for Pulse spirit")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


PULSE_SYSTEM_PROMPT = """\
You are a knowledge graph synthesis agent for an organization using shared memory.
Given a session's activity data and context from related sessions and quests, produce structured synthesis.

Output ONLY valid JSON with these fields:

- edges: array of objects with {type, target_id, confidence, reason}
  - CONTINUES: this session continues a previous session's work (target_id = previous session id)
  - INVOLVES: this session relates to a quest (target_id = quest id)
  Only include edges with confidence >= 0.6.

- signals: array of objects with {type, people, topic, confidence, message}
  - convergence: 2+ people independently approaching same topic from different angles
  - tension: contradictory decisions or directions detected across sessions
  - synthesis_deficit: a topic has many sessions/findings but few or no pattern artifacts — synthesis is missing
  Only include signals with confidence >= 0.7. Be conservative — false positives are worse than missed signals.

- recommendations: array of strings — concrete next actions. Examples:
  - "Run /deep-reflect on [topic] — 5 sessions touched it, no pattern exists yet"
  - "The [quest] quest has diverging threads — consider a synthesis session"
  Keep to 0-2 items. Only include genuinely useful suggestions.

- brief: 1-2 sentence summary for the person's next session greeting. Focus on what they left off doing and what changed since. Be specific, not generic.

If nothing interesting emerged, return empty arrays and a simple brief.\
"""


async def synthesize_session(session_data: dict) -> dict:
    """Synthesize a session's contribution to organizational knowledge.

    Args:
        session_data: Dict with session_id, author, topic, branch, tools_used,
                      files_touched, tool_count, related_sessions, active_quests

    Returns:
        Dict with edges, signals, and brief
    """
    client = _get_client()

    # Build context message
    parts = []
    parts.append(f"Session: {session_data.get('session_id', 'unknown')}")
    parts.append(f"Author: {session_data.get('author', 'unknown')}")
    if session_data.get("topic"):
        parts.append(f"Topic: {session_data['topic']}")
    if session_data.get("branch"):
        parts.append(f"Branch: {session_data['branch']}")

    if session_data.get("tools_used"):
        parts.append(f"Tools used: {', '.join(session_data['tools_used'][:20])}")
    if session_data.get("files_touched"):
        # Truncate paths to last 2 segments for brevity
        paths = session_data["files_touched"][:30]
        short_paths = ["/".join(p.split("/")[-2:]) for p in paths]
        parts.append(f"Files touched: {', '.join(short_paths)}")
    if session_data.get("tool_count"):
        parts.append(f"Total tool invocations: {session_data['tool_count']}")

    related = session_data.get("related_sessions", [])
    if related:
        parts.append("\n--- Same person's recent sessions ---")
        for s in related[:5]:
            line = f"- {s.get('id', '?')}: {s.get('topic', 'untitled')} ({s.get('date', '?')})"
            if s.get("summary"):
                line += f" — {s['summary'][:100]}"
            parts.append(line)

    others = session_data.get("other_sessions", [])
    if others:
        parts.append("\n--- Other people's recent sessions ---")
        for s in others[:5]:
            parts.append(f"- {s.get('author', '?')}: {s.get('topic', 'untitled')} ({s.get('date', '?')})")

    quests = session_data.get("active_quests", [])
    if quests:
        parts.append("\n--- Active quests ---")
        for q in quests[:10]:
            topics = ", ".join(q.get("topics", [])[:5]) if q.get("topics") else "no topics"
            parts.append(f"- {q.get('id', '?')}: {q.get('title', 'untitled')} [{topics}]")

    # Full session transcript — the primary context for synthesis
    transcript = session_data.get("transcript", "")
    if transcript:
        parts.append("\n--- Session transcript ---")
        parts.append(transcript)

    # Raw observation lines as supplementary signal
    obs_raw = session_data.get("obs_raw", [])
    if obs_raw:
        parts.append("\n--- Raw observation log (recent) ---")
        for line in obs_raw[-50:]:
            parts.append(line)

    user_message = "\n".join(parts)

    # Full transcript up to 100K tokens (~400K chars). Sonnet 4.6 handles 1M context.
    MAX_CHARS = 400_000
    if len(user_message) > MAX_CHARS:
        user_message = user_message[:MAX_CHARS] + "\n\n[truncated]"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=PULSE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = response.content[0].text.strip()

    # Parse JSON response — be defensive
    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown code blocks
        try:
            if "```" in raw_text:
                json_part = raw_text.split("```")[1]
                if json_part.startswith("json"):
                    json_part = json_part[4:]
                result = json.loads(json_part.strip())
            else:
                raise json.JSONDecodeError("no code block", raw_text, 0)
        except (json.JSONDecodeError, IndexError):
            logger.warning("[PULSE] Failed to parse JSON response: %s", raw_text[:200])
            result = {"edges": [], "signals": [], "brief": "Session completed."}

    # Validate structure
    if not isinstance(result.get("edges"), list):
        result["edges"] = []
    if not isinstance(result.get("signals"), list):
        result["signals"] = []
    if not isinstance(result.get("recommendations"), list):
        result["recommendations"] = []
    if not isinstance(result.get("brief"), str):
        result["brief"] = "Session completed."

    logger.info(
        "[PULSE] Synthesized session '%s': %d edges, %d signals",
        session_data.get("session_id", "?"),
        len(result["edges"]),
        len(result["signals"]),
    )

    return result
