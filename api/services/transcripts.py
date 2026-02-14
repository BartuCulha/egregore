"""Transcript collection service for the CASS distillation pipeline.

Handles dedup checking, storage, and Neo4j indexing for session transcripts.
Storage strategy:
  - CL internal: gzipped JSONL committed to Curve-Labs/egregore-transcripts (private Git repo).
  - Customer orgs: uploaded to Supabase Storage bucket `egregore-transcripts`.
Both paths index metadata in Neo4j via the same index_transcript() function.
"""

import logging
import os
from datetime import datetime, timezone

from .graph import execute_query

logger = logging.getLogger(__name__)

# CL's org slug — used to detect internal vs customer path
CL_SLUG = "curvelabs"


async def check_duplicate(org: dict, session_id: str) -> bool:
    """Check if a transcript with this session_id already exists in Neo4j."""
    result = await execute_query(org, """
        MATCH (t:Transcript {sessionId: $sessionId})
        RETURN t.sessionId
    """, {"sessionId": session_id})
    values = result.get("values", [])
    return bool(values and values[0])


def _compute_storage_path(slug: str, session_id: str, started_at: str) -> str:
    """Compute the storage path for a transcript."""
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            month = dt.strftime("%Y-%m")
        except (ValueError, AttributeError):
            month = datetime.now(timezone.utc).strftime("%Y-%m")
    else:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"transcripts/{slug}/{month}/{session_id}.jsonl.gz"


async def store_transcript(org: dict, session_id: str, file_data: bytes, metadata: dict) -> str:
    """Store a gzipped transcript.

    CL internal orgs: write to staging directory (Git repo handles the rest).
    Customer orgs: upload to Supabase Storage bucket.

    Returns the storage path.
    """
    slug = org.get("slug", "unknown")
    started_at = metadata.get("started_at", "")
    rel_path = _compute_storage_path(slug, session_id, started_at)

    if slug == CL_SLUG:
        # CL internal: write to staging dir (git commit/push handled by bash scripts)
        from pathlib import Path
        staging_dir = Path(os.environ.get("TRANSCRIPT_STAGING_DIR", "/tmp/egregore-transcripts"))
        full_path = staging_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(file_data)
    else:
        # Customer orgs: upload to Supabase Storage
        from .supabase import upload_transcript
        upload_transcript(slug, rel_path, file_data)

    return rel_path


async def index_transcript(org: dict, metadata: dict) -> dict:
    """Create or update a Transcript node in Neo4j with relationships."""
    result = await execute_query(org, """
        MERGE (t:Transcript {sessionId: $sessionId})
        SET t.author = $author,
            t.branch = $branch,
            t.startedAt = $startedAt,
            t.endedAt = $endedAt,
            t.messageCount = $messageCount,
            t.sizeBytes = $sizeBytes,
            t.storagePath = $storagePath,
            t.status = 'uploaded'
        WITH t
        OPTIONAL MATCH (p:Person {github: $author})
        FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
            MERGE (t)-[:BY]->(p)
        )
        RETURN t.sessionId
    """, {
        "sessionId": metadata["session_id"],
        "author": metadata.get("author", ""),
        "branch": metadata.get("branch", ""),
        "startedAt": metadata.get("started_at", ""),
        "endedAt": metadata.get("ended_at", ""),
        "messageCount": metadata.get("message_count", 0),
        "sizeBytes": metadata.get("size_bytes", 0),
        "storagePath": metadata.get("storage_path", ""),
    })
    return result
