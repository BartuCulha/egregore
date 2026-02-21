-- Migration: Add onboarding harvest + consent fields to memberships
-- Date: 2026-02-20
-- Context: Onboarding seed architecture — store role, focus, work style,
--          and consent preferences per-org on memberships.

-- display_name was already used in code but missing from schema
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Onboarding harvest fields
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS member_role TEXT;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS focus TEXT;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS work_style TEXT;

-- Consent fields (default to opt-in, matching current behavior)
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS consent_session_tracking BOOLEAN DEFAULT true;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS consent_transcript_sharing BOOLEAN DEFAULT true;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS consent_telemetry BOOLEAN DEFAULT true;
ALTER TABLE memberships ADD COLUMN IF NOT EXISTS contact_preference TEXT DEFAULT 'all';
