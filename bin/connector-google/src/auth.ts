/** Google auth flow — OAuth2 with local callback server or hosted API. */

import { createServer } from "http";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "fs";
import { join } from "path";
import { homedir } from "os";
import { google } from "googleapis";
import type { AuthStatus } from "./types.js";
import { writeState, readState, isHosted, getApiUrl, getApiKey } from "./config.js";

const TOKEN_DIR = join(homedir(), ".egregore", "context", "google");
const TOKEN_PATH = join(TOKEN_DIR, ".tokens.json");

// Google OAuth client IDs for installed (desktop) applications.
// These are not secrets — they're embedded in any desktop OAuth app.
// Users can override via GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars.
const DEFAULT_CLIENT_ID = process.env.GOOGLE_CLIENT_ID ?? "";
const DEFAULT_CLIENT_SECRET = process.env.GOOGLE_CLIENT_SECRET ?? "";

const SCOPES = [
  "https://www.googleapis.com/auth/drive.readonly",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/calendar.readonly",
  "https://www.googleapis.com/auth/documents.readonly",
  "https://www.googleapis.com/auth/spreadsheets.readonly",
];

const REDIRECT_PORT = 8095;
const REDIRECT_URI = `http://localhost:${REDIRECT_PORT}/callback`;

function createOAuth2Client() {
  if (!DEFAULT_CLIENT_ID || !DEFAULT_CLIENT_SECRET) {
    throw new Error(
      "Google OAuth credentials not configured.\n" +
        "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your .env file.\n" +
        "Create them at https://console.cloud.google.com/apis/credentials"
    );
  }
  return new google.auth.OAuth2(DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET, REDIRECT_URI);
}

/**
 * Get an authenticated OAuth2 client. Loads saved tokens.
 * Throws if not authenticated.
 */
export function getAuthClient(): InstanceType<typeof google.auth.OAuth2> {
  const client = createOAuth2Client();
  const tokens = loadTokens();
  if (!tokens) {
    throw new Error("Not authenticated. Run 'auth' first.");
  }
  client.setCredentials(tokens);

  // Auto-refresh: googleapis handles token refresh automatically when
  // refresh_token is set and access_token is expired.
  client.on("tokens", (newTokens) => {
    const existing = loadTokens() ?? {};
    saveTokens({ ...existing, ...newTokens });
  });

  return client;
}

function loadTokens(): Record<string, unknown> | null {
  if (!existsSync(TOKEN_PATH)) return null;
  try {
    return JSON.parse(readFileSync(TOKEN_PATH, "utf-8"));
  } catch {
    return null;
  }
}

function saveTokens(tokens: Record<string, unknown>): void {
  mkdirSync(TOKEN_DIR, { recursive: true });
  writeFileSync(TOKEN_PATH, JSON.stringify(tokens, null, 2), { mode: 0o600 });
}

function clearTokens(): void {
  if (existsSync(TOKEN_PATH)) {
    writeFileSync(TOKEN_PATH, "{}", { mode: 0o600 });
  }
}

/**
 * Run OAuth setup — opens browser for consent (local path).
 * For hosted (Coder), prints API OAuth URL instead.
 */
export async function authSetup(): Promise<void> {
  if (isHosted()) {
    return authHosted();
  }

  const client = createOAuth2Client();
  const authUrl = client.generateAuthUrl({
    access_type: "offline",
    scope: SCOPES,
    prompt: "consent",
  });

  console.log("Opening Google OAuth consent flow...\n");

  // Start local server to receive callback
  const code = await new Promise<string>((resolve, reject) => {
    const server = createServer((req, res) => {
      const url = new URL(req.url ?? "/", `http://localhost:${REDIRECT_PORT}`);
      if (url.pathname === "/callback") {
        const code = url.searchParams.get("code");
        if (code) {
          res.writeHead(200, { "Content-Type": "text/html" });
          res.end("<html><body><h2>Authenticated! You can close this tab.</h2></body></html>");
          server.close();
          resolve(code);
        } else {
          const error = url.searchParams.get("error") ?? "No code received";
          res.writeHead(400, { "Content-Type": "text/html" });
          res.end(`<html><body><h2>Error: ${error}</h2></body></html>`);
          server.close();
          reject(new Error(error));
        }
      }
    });

    server.listen(REDIRECT_PORT, () => {
      console.log(`Listening on port ${REDIRECT_PORT} for OAuth callback...`);
      console.log(`\nOpen this URL in your browser:\n\n  ${authUrl}\n`);

      // Try to open browser
      import("open")
        .then((mod) => mod.default(authUrl))
        .catch(() => {
          // Browser open failed — URL is already printed
        });
    });

    // Timeout after 2 minutes
    setTimeout(() => {
      server.close();
      reject(new Error("OAuth callback timed out after 2 minutes"));
    }, 120_000);
  });

  // Exchange code for tokens
  const { tokens } = await client.getToken(code);
  saveTokens(tokens as Record<string, unknown>);
  client.setCredentials(tokens);

  // Get user email
  const oauth2 = google.oauth2({ version: "v2", auth: client });
  const userInfo = await oauth2.userinfo.get();
  const email = userInfo.data.email ?? "";

  writeState({
    google_connector: true,
    google_auth_complete: true,
    google_account: email,
  });

  console.log(`\nConnected as ${email}`);
}

/**
 * Check current auth status.
 */
export async function authStatus(): Promise<AuthStatus> {
  const tokens = loadTokens();
  if (!tokens || (!tokens.access_token && !tokens.refresh_token)) {
    return { connected: false };
  }

  try {
    const client = getAuthClient();
    const oauth2 = google.oauth2({ version: "v2", auth: client });
    const userInfo = await oauth2.userinfo.get();
    return { connected: true, account: userInfo.data.email ?? undefined };
  } catch (err: unknown) {
    const e = err as { message?: string };
    // If we have a refresh token, we might just need a refresh
    if (tokens.refresh_token) {
      return { connected: true, account: readState().google_account ?? "unknown (token needs refresh)" };
    }
    return { connected: false, error: e.message };
  }
}

/**
 * Revoke Google access.
 */
export async function authRevoke(): Promise<void> {
  const tokens = loadTokens();
  if (tokens?.access_token) {
    try {
      const client = createOAuth2Client();
      client.setCredentials(tokens);
      await client.revokeCredentials();
    } catch {
      // May fail if token already expired
    }
  }

  clearTokens();
  writeState({
    google_connector: false,
    google_auth_complete: false,
    google_account: undefined,
    google_services: undefined,
    google_last_sync: undefined,
  });

  console.log("Google access revoked. Auth tokens cleared.");
}

/**
 * Hosted path — guide user to API OAuth URL (for Coder workspaces).
 */
async function authHosted(): Promise<void> {
  const apiUrl = getApiUrl();
  const apiKey = getApiKey();

  if (!apiUrl || !apiKey) {
    console.error("API URL or API key not configured. Check egregore.json and .env");
    process.exit(1);
  }

  try {
    const resp = await fetch(`${apiUrl}/api/connectors/google/auth-url`, {
      headers: { Authorization: `Bearer ${apiKey}` },
    });

    if (!resp.ok) {
      console.error("Failed to get OAuth URL from API. Google connector may not be configured on the server.");
      console.error(`Status: ${resp.status}`);
      process.exit(1);
    }

    const data = (await resp.json()) as { url: string };
    console.log("\nOpen this URL in your browser to authenticate:");
    console.log(`\n  ${data.url}\n`);
    console.log("After authorizing, run 'auth status' to verify the connection.");
  } catch (err: unknown) {
    const e = err as { message?: string };
    console.error(`Failed to reach API: ${e.message}`);
    process.exit(1);
  }
}

/**
 * Print auth status to stdout.
 */
export async function printAuthStatus(): Promise<void> {
  const status = await authStatus();
  const state = readState();

  if (status.connected) {
    console.log("Status: connected");
    console.log(`Account: ${status.account || state.google_account || "unknown"}`);
    if (state.google_services?.length) {
      console.log(`Services: ${state.google_services.join(", ")}`);
    }
    if (state.google_last_sync) {
      console.log(`Last sync: ${state.google_last_sync}`);
    }
  } else {
    console.log("Status: not connected");
    if (status.error) {
      console.log(`Error: ${status.error}`);
    }
    console.log("\nRun 'auth' to connect your Google account.");
  }
}
