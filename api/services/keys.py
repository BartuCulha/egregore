"""
User API key management — encrypt/decrypt/store user-provided keys.

Keys are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256).
The encryption key is derived from EGREGORE_KEY_SECRET env var.
"""

import os
import base64
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Derive a Fernet-compatible key from the server secret
_KEY_SECRET = os.environ.get("EGREGORE_KEY_SECRET", "")


def _get_fernet():
    """Lazy-load Fernet to avoid import at module level if not needed."""
    from cryptography.fernet import Fernet

    if not _KEY_SECRET:
        raise RuntimeError("EGREGORE_KEY_SECRET not set — cannot encrypt user keys")

    # Derive a 32-byte key from the secret, then base64-encode for Fernet
    derived = hashlib.sha256(_KEY_SECRET.encode()).digest()
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def encrypt_key(plaintext: str) -> str:
    """Encrypt an API key for storage."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    """Decrypt an API key from storage."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()


def store_user_key(github_username: str, key_name: str, plaintext: str) -> bool:
    """Encrypt and store a user's API key in Supabase."""
    from .supabase import get_client, get_user_by_github

    user = get_user_by_github(github_username)
    if not user:
        logger.warning(f"User not found: {github_username}")
        return False

    encrypted = encrypt_key(plaintext)

    update = {
        f"{key_name}_enc": encrypted,
        f"{key_name}_set": True,
        "keys_updated_at": "now()",
    }

    get_client().table("users").update(update).eq(
        "github_username", github_username
    ).execute()

    logger.info(f"Stored {key_name} for {github_username}")
    return True


def get_user_key(github_username: str, key_name: str) -> Optional[str]:
    """Retrieve and decrypt a user's API key from Supabase."""
    from .supabase import get_client

    result = (
        get_client()
        .table("users")
        .select(f"{key_name}_enc, {key_name}_set")
        .eq("github_username", github_username)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    row = result.data[0]
    enc = row.get(f"{key_name}_enc")
    if not enc:
        return None

    try:
        return decrypt_key(enc)
    except Exception as e:
        logger.error(f"Failed to decrypt {key_name} for {github_username}: {e}")
        return None


def delete_user_key(github_username: str, key_name: str) -> bool:
    """Delete a user's stored API key."""
    from .supabase import get_client

    get_client().table("users").update({
        f"{key_name}_enc": None,
        f"{key_name}_set": False,
        "keys_updated_at": "now()",
    }).eq("github_username", github_username).execute()

    logger.info(f"Deleted {key_name} for {github_username}")
    return True


def get_user_key_status(github_username: str) -> dict:
    """Check which keys a user has set (without decrypting)."""
    from .supabase import get_client

    result = (
        get_client()
        .table("users")
        .select("anthropic_api_key_set, keys_updated_at")
        .eq("github_username", github_username)
        .limit(1)
        .execute()
    )

    if not result.data:
        return {"anthropic_api_key": False}

    row = result.data[0]
    return {
        "anthropic_api_key": bool(row.get("anthropic_api_key_set")),
        "updated_at": row.get("keys_updated_at"),
    }
