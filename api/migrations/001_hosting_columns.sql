-- Add hosting columns to orgs table for Coder VPS tracking
-- Run this in Supabase SQL Editor before enabling hosting features

ALTER TABLE orgs ADD COLUMN IF NOT EXISTS hosting_enabled boolean DEFAULT false;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS hosting_ip text;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS hosting_server_id text;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS hosting_coder_url text;
ALTER TABLE orgs ADD COLUMN IF NOT EXISTS hosting_coder_token text;
