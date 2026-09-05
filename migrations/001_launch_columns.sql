-- Migration: add launch-broadcast columns to waitlist_users
-- Run this ONCE in Supabase SQL Editor. Do NOT re-run schema.sql (it drops the table).
ALTER TABLE waitlist_users
  ADD COLUMN IF NOT EXISTS signup_token    uuid UNIQUE DEFAULT gen_random_uuid(),
  ADD COLUMN IF NOT EXISTS token_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS granted_months  int,
  ADD COLUMN IF NOT EXISTS notified_at     timestamptz,
  ADD COLUMN IF NOT EXISTS converted_at    timestamptz;

-- Backfill signup_token for any existing rows that got NULL
-- (happens if the DB engine didn't evaluate the volatile default for old rows)
UPDATE waitlist_users
SET signup_token = gen_random_uuid()
WHERE signup_token IS NULL;
