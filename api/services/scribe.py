"""Scribe spirit — summarizes knowledge artifacts using Claude.

The Scribe reads artifact content and produces 2-3 sentence summaries
focused on key insights, decisions, or findings.
"""

import os
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

# Org-level key — Scribe is org infrastructure, not per-user
_client = None

def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — required for Scribe spirit")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


SCRIBE_SYSTEM_PROMPT = (
    "Summarize this knowledge artifact in 2-3 sentences. "
    "Focus on the key insight, decision, or finding. "
    "Be specific — mention names, technologies, and concrete outcomes."
)


async def summarize_artifact(title: str, content: str, artifact_type: str = "document") -> str:
    """Summarize an artifact using Claude Sonnet.

    Args:
        title: Artifact title
        content: Full text content of the artifact
        artifact_type: Type hint (document, decision, pattern, etc.)

    Returns:
        2-3 sentence summary string
    """
    client = _get_client()

    # Truncate content to ~2000 chars to control costs (~500 input tokens)
    truncated = content[:2000]
    if len(content) > 2000:
        truncated += "\n\n[truncated]"

    user_message = f"Title: {title}\nType: {artifact_type}\n\n{truncated}"

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=SCRIBE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    summary = response.content[0].text.strip()
    logger.info("[SCRIBE] Summarized '%s' (%d chars -> %d chars)", title, len(content), len(summary))
    return summary
