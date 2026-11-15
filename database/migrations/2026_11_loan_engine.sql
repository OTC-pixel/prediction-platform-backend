-- =====================================================================
--  Loan engine migration  (Postgres / Supabase)
--
--  What it does
--    * creates the missing loan_endorsements table
--    * adds the loan rules columns to loan_config
--    * adds due date / rules snapshot / late-fee markers to loans
--    * creates loan_charges (fee ledger) and loan_topups
--    * gives loans that are ALREADY OUT the same rules they were issued
--      under and a fresh full 30-day term from the moment you run this
--      (never a due date in the past, so nobody gets a late fee on day one)
--
--  Safe to run more than once. Nothing is dropped or rewritten except
--  the backfill of loans currently out. It is one transaction: if any
--  statement fails, nothing is applied.
--
--  Before you run it:  take a backup (Supabase: Database > Backups, or
--  pg_dump).  Then run this file, THEN deploy the new backend.
--
--  How to run it
--    Supabase : SQL Editor > New query > paste this whole file > Run
--    psql     : psql "$DATABASE_URL" -f 2026_11_loan_engine.sql
-- =====================================================================

BEGIN;

-- Pre-flight: stop early, with a clear message, if this is the wrong database.
DO $$
BEGIN
    IF to_regclass('public.loans') IS NULL
       OR to_regclass('public.loan_config') IS NULL
       OR to_regclass('public.loan_repayments') IS NULL
       OR to_regclass('public.users') IS NULL THEN
        RAISE EXCEPTION 'Expected tables users, loans, loan_config and loan_repayments in schema public were not all found. Is this the right database?';
    END IF;
END $$;

-- 1. Peer endorsements. The app has always read and written this table but
--    the schema script never created it. IF NOT EXISTS keeps any copy you
--    already made by hand.
CREATE TABLE IF NOT EXISTS loan_endorsements (
    id SERIAL PRIMARY KEY,
    loan_id INTEGER NOT NULL REFERENCES loans(id),
    endorser_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (loan_id, endorser_user_id)
);

-- 2. Rules the Treasurer sets. interest_rate stays the main fee %; the three
--    optional fee columns are NULL = "same as the main fee"; a NULL
--    starter_limit switches the tiered limit off (75% of savings only).
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS term_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS topup_window_days INTEGER NOT NULL DEFAULT 20;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS max_topups INTEGER NOT NULL DEFAULT 2;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS topup_fee_percent NUMERIC(5, 2);
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS extension_fee_percent NUMERIC(5, 2);
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS extension_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS max_extensions INTEGER NOT NULL DEFAULT 1;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS late_fee_percent NUMERIC(5, 2);
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS default_after_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS starter_limit NUMERIC(12, 2);
ALTER TABLE loan_config ADD COLUMN IF NOT EXISTS limit_step_percent NUMERIC(5, 2) NOT NULL DEFAULT 50;

-- 3. Per loan: the rules in force when it was paid out (a later rule change
--    never rewrites a loan already out), its due date, and the moments that
--    decide whether it counts as repaid on time.
ALTER TABLE loans ADD COLUMN IF NOT EXISTS due_at TIMESTAMPTZ;
ALTER TABLE loans ADD COLUMN IF NOT EXISTS rules JSONB;
ALTER TABLE loans ADD COLUMN IF NOT EXISTS late_fee_applied_at TIMESTAMPTZ;
ALTER TABLE loans ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;

-- 4. Append-only extras on top of principal + the base fee: top-up
--    principal, top-up fees, extension fees, late fees.
CREATE TABLE IF NOT EXISTS loan_charges (
    id SERIAL PRIMARY KEY,
    loan_id INTEGER NOT NULL REFERENCES loans(id),
    kind TEXT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_loan_charges_loan ON loan_charges (loan_id);

CREATE TABLE IF NOT EXISTS loan_topups (
    id SERIAL PRIMARY KEY,
    loan_id INTEGER NOT NULL REFERENCES loans(id),
    amount NUMERIC(12, 2) NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_by INTEGER REFERENCES users(id),
    decided_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_loan_topups_loan ON loan_topups (loan_id);

-- 5. Loans already out when this runs: same rules they were issued under
--    (their own fee for every fee type) and a fresh full term from now.
UPDATE loans
SET rules = jsonb_build_object(
        'term_days', 30, 'topup_window_days', 20, 'max_topups', 2,
        'topup_fee_percent', COALESCE(interest_rate, 0),
        'extension_fee_percent', COALESCE(interest_rate, 0),
        'extension_days', 30, 'max_extensions', 1,
        'late_fee_percent', COALESCE(interest_rate, 0),
        'default_after_days', 30),
    due_at = GREATEST(COALESCE(disbursed_at, NOW()), NOW()) + INTERVAL '30 days'
WHERE status = 'disbursed' AND rules IS NULL;

COMMIT;

-- ---------------------------------------------------------------------
--  OPTIONAL, Supabase only: lock the new tables away from Supabase's public
--  REST API. Tables created in SQL are open to it unless row level security
--  is on. The backend connects directly as the database owner, which is not
--  affected by RLS, so enabling it (with no policies) simply blocks API
--  access. If your other tables already have RLS enabled, do the same here;
--  if they do not, you probably have the same exposure on all of them and
--  that is worth fixing separately. Uncomment to apply:
-- ---------------------------------------------------------------------
-- ALTER TABLE loan_endorsements ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE loan_charges      ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE loan_topups       ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
--  Check it worked (read-only; run after the COMMIT above)
-- ---------------------------------------------------------------------
-- Loans that are out should each have a due date and rules:
--   SELECT id, user_id, principal, due_at::date AS due, rules IS NOT NULL AS has_rules
--   FROM loans WHERE status = 'disbursed' ORDER BY id;
-- The new tables should exist (all four rows should say true):
--   SELECT to_regclass('loan_endorsements') IS NOT NULL AS endorsements,
--          to_regclass('loan_charges')      IS NOT NULL AS charges,
--          to_regclass('loan_topups')       IS NOT NULL AS topups,
--          EXISTS (SELECT 1 FROM information_schema.columns
--                  WHERE table_name = 'loans' AND column_name = 'due_at') AS due_at;
