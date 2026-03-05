-- Migration 003: Google Connector — add OAuth token columns to users table
-- Run this on existing Supabase instances to enable the Google connector.

ALTER TABLE users ADD COLUMN IF NOT EXISTS google_oauth_token_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_oauth_token_set BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token_set BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_account_email TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS keys_updated_at TIMESTAMPTZ;
