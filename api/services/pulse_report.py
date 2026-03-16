"""Pulse Weekly Report — deep synthesis of a week's Pulse runs.

Takes the full corpus of runs (payloads + responses) and produces a
narrative assessment: what the organization worked on, what patterns
emerged, what convergences Pulse detected, and what's missing.
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
            raise RuntimeError("ANTHROPIC_API_KEY not set — required for Pulse report")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


REPORT_SYSTEM_PROMPT = """\
You are the Pulse — an autonomous intelligence layer for an organization using shared memory and a knowledge graph. You run after every session and synthesize what happened.

You are now producing your WEEKLY REPORT. You have access to every Pulse run from the past week — the full context each session generated (who worked, what they touched, which quests were active) and the synthesis you produced (edges, signals, brief).

Write a deep assessment in first person (you ARE the Pulse). Structure it as:

1. **What happened this week** — narrative overview of the organization's work. Who worked on what. How sessions connected to each other. Not a list of sessions, but a story of how the work flowed.

2. **Patterns I detected** — convergences, recurring themes, topics that multiple people touched independently. Be specific — name the people, the quests, the artifacts. Say what the pattern means, not just that it exists.

3. **Tensions and gaps** — contradictions between sessions, synthesis deficits (topics with lots of activity but no crystallized patterns), stale quests that got attention but no resolution. Be honest about what's unresolved.

4. **What I recommend** — 2-3 concrete actions. Not generic advice. Specific: "Run /deep-reflect on X", "Cem and Oz should sync on Y", "The Z quest needs a decision before more sessions pile up."

Keep it under 3000 characters (Telegram limit). Be opinionated. You have a perspective — use it. Don't hedge with "it seems" or "perhaps". Say what you see.\
"""


async def synthesize_report(runs: list[dict], period_days: int = 7) -> str:
    """Synthesize a week's Pulse runs into a narrative report.

    Args:
        runs: List of {session_id, timestamp, payload, response} dicts
        period_days: Number of days covered

    Returns:
        Narrative report string
    """
    client = _get_client()

    # Build the context from all runs
    parts = [f"Period: last {period_days} days | Sessions: {len(runs)}\n"]

    for run in runs:
        sid = run.get("session_id", "?")
        ts = run.get("timestamp", "?")
        payload = run.get("payload", {})
        response = run.get("response", {})

        author = payload.get("author", "?")
        topic = payload.get("topic", "untitled")
        branch = payload.get("branch", "")
        tools = payload.get("tools_used", [])
        files = payload.get("files_touched", [])
        tool_count = payload.get("tool_count", 0)

        # Related sessions context
        related = payload.get("related_sessions", [])
        others = payload.get("other_sessions", [])

        # Pulse synthesis output
        edges = response.get("edges", [])
        signals = response.get("signals", [])
        brief = response.get("brief", "")
        recs = response.get("recommendations", [])

        parts.append(f"--- Session: {sid} ({ts}) ---")
        parts.append(f"Author: {author} | Topic: {topic} | Branch: {branch}")
        parts.append(f"Activity: {tool_count} tool calls, {len(files)} files ({', '.join(tools)})")

        if files:
            short_files = ["/".join(f.split("/")[-2:]) for f in files[:10]]
            parts.append(f"Files: {', '.join(short_files)}")

        if edges:
            for e in edges:
                parts.append(f"  Edge: {e.get('type')} → {e.get('target_id')} (conf={e.get('confidence')})")
                if e.get("reason"):
                    parts.append(f"    Reason: {e['reason']}")

        if signals:
            for s in signals:
                parts.append(f"  Signal: {s.get('type')} [{', '.join(s.get('people', []))}] — {s.get('message', '')}")

        if brief:
            parts.append(f"  Brief: {brief}")

        if recs:
            for r in recs:
                parts.append(f"  Rec: {r}")

        parts.append("")

    user_message = "\n".join(parts)

    # Sonnet 4.6 with generous context — this is a weekly synthesis
    if len(user_message) > 30000:
        user_message = user_message[:30000] + "\n\n[truncated — older sessions omitted]"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=REPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    report = response.content[0].text.strip()
    logger.info("[PULSE-REPORT] Generated %d-day report from %d runs (%d chars)", period_days, len(runs), len(report))
    return report
