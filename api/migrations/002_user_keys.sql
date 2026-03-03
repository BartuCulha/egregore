-- Add API key storage to users table
-- Keys are stored encrypted (the API encrypts/decrypts, not Supabase)
-- Run this in Supabase SQL Editor

ALTER TABLE users ADD COLUMN IF NOT EXISTS anthropic_api_key_enc text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS anthropic_api_key_set boolean DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS keys_updated_at timestamptz;
