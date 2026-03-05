"""
Google Workspace Connector — server-side service for hosted deployments.

Used by Coder workspaces where gws CLI can't run locally (no browser/keyring).
Uses google-api-python-client for API access with server-stored tokens.
"""

import os
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://egregore-production-55f2.up.railway.app/api/connectors/google/callback",
)

# Scopes needed for each service
SCOPES_BY_SERVICE = {
    "drive": ["https://www.googleapis.com/auth/drive.readonly"],
    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.readonly"],
    "docs": ["https://www.googleapis.com/auth/documents.readonly"],
    "sheets": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
}

ALL_SCOPES = list({s for scopes in SCOPES_BY_SERVICE.values() for s in scopes})


def get_auth_url(state: str = "") -> str:
    """Generate Google OAuth consent URL."""
    if not GOOGLE_CLIENT_ID:
        raise ValueError("GOOGLE_CLIENT_ID not configured on server")

    from urllib.parse import urlencode

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(ALL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state

    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": GOOGLE_REDIRECT_URI,
            },
        )

        if resp.status_code != 200:
            logger.error(f"Token exchange failed: {resp.status_code} {resp.text}")
            return {"error": f"Token exchange failed: {resp.status_code}"}

        tokens = resp.json()

        # Get user email
        email = ""
        access_token = tokens.get("access_token", "")
        if access_token:
            user_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if user_resp.status_code == 200:
                email = user_resp.json().get("email", "")

        return {
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token", ""),
            "email": email,
            "expires_in": tokens.get("expires_in", 3600),
        }


async def refresh_access_token(refresh_token: str) -> Optional[str]:
    """Refresh an expired access token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.error(f"Token refresh failed: {resp.status_code}")
        return None


async def revoke_token(token: str) -> bool:
    """Revoke a Google OAuth token."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
        )
        return resp.status_code == 200


class GoogleWorkspaceClient:
    """Server-side Google API client for hosted deployments."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def list_drive_files(
        self, page_size: int = 25, query: str = "", folder_id: str = ""
    ) -> dict:
        """List Drive files."""
        q = "trashed = false"
        if query:
            q += f" and fullText contains '{query}'"
        if folder_id:
            q += f" and '{folder_id}' in parents"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                headers=self._headers,
                params={
                    "pageSize": page_size,
                    "q": q,
                    "fields": "files(id,name,mimeType,modifiedTime,owners,webViewLink,parents),nextPageToken",
                    "orderBy": "modifiedTime desc",
                },
            )
            if resp.status_code != 200:
                return {"error": resp.text, "files": []}
            return resp.json()

    async def get_drive_file(self, file_id: str) -> dict:
        """Get Drive file metadata."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers=self._headers,
                params={
                    "fields": "id,name,mimeType,modifiedTime,owners,webViewLink,parents",
                },
            )
            if resp.status_code != 200:
                return {"error": resp.text}
            return resp.json()

    async def export_drive_file(self, file_id: str, mime_type: str = "text/plain") -> str:
        """Export a Google Workspace file (Docs, Sheets) to the given format."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers=self._headers,
                params={"mimeType": mime_type},
            )
            if resp.status_code != 200:
                return ""
            return resp.text

    async def list_gmail(self, max_results: int = 10, query: str = "") -> dict:
        """List Gmail messages."""
        async with httpx.AsyncClient(timeout=15) as client:
            params: dict = {"userId": "me", "maxResults": max_results}
            if query:
                params["q"] = query
            resp = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=self._headers,
                params=params,
            )
            if resp.status_code != 200:
                return {"error": resp.text, "messages": []}
            return resp.json()

    async def get_gmail_message(self, message_id: str, fmt: str = "full") -> dict:
        """Get a single Gmail message."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
                headers=self._headers,
                params={"format": fmt},
            )
            if resp.status_code != 200:
                return {"error": resp.text}
            return resp.json()

    async def list_calendar_events(
        self, calendar_id: str = "primary", time_min: str = "", time_max: str = ""
    ) -> dict:
        """List calendar events."""
        async with httpx.AsyncClient(timeout=15) as client:
            params: dict = {
                "calendarId": calendar_id,
                "maxResults": 50,
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
            resp = await client.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers=self._headers,
                params=params,
            )
            if resp.status_code != 200:
                return {"error": resp.text, "items": []}
            return resp.json()

    async def get_spreadsheet(self, spreadsheet_id: str, range_: str = "") -> dict:
        """Get spreadsheet data."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
                headers=self._headers,
            )
            if resp.status_code != 200:
                return {"error": resp.text}
            meta = resp.json()

            # Get values if range specified
            if range_:
                val_resp = await client.get(
                    f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_}",
                    headers=self._headers,
                )
                if val_resp.status_code == 200:
                    meta["values"] = val_resp.json().get("values", [])

            return meta
