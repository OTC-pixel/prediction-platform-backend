-- =====================================================================
--  ROLLBACK for 2026_11_loan_engine.sql   -- use only if you must
--
--  WARNING: this DELETES the data the loan engine has recorded since you
--  migrated (top-up / extension / late-fee ledger lines, top-up requests,
--  due dates and rules on loans). It does not touch savings, payments,
--  or the loans themselves. Only run it together with putting the OLD
--  backend back, because the old code cannot read the ledger: any loan
--  with top-ups or fees would then show the wrong balance.
--
--  loan_endorsements is deliberately NOT dropped (it holds real
--  endorsements and the old code needs it too).
--
--  Take a backup first.
-- =====================================================================

BEGIN;

DROP TABLE IF EXISTS loan_topups;
DROP TABLE IF EXISTS loan_charges;

ALTER TABLE loans DROP COLUMN IF EXISTS settled_at;
ALTER TABLE loans DROP COLUMN IF EXISTS late_fee_applied_at;
ALTER TABLE loans DROP COLUMN IF EXISTS rules;
ALTER TABLE loans DROP COLUMN IF EXISTS due_at;

ALTER TABLE loan_config DROP COLUMN IF EXISTS limit_step_percent;
ALTER TABLE loan_config DROP COLUMN IF EXISTS starter_limit;
ALTER TABLE loan_config DROP COLUMN IF EXISTS default_after_days;
ALTER TABLE loan_config DROP COLUMN IF EXISTS late_fee_percent;
ALTER TABLE loan_config DROP COLUMN IF EXISTS max_extensions;
ALTER TABLE loan_config DROP COLUMN IF EXISTS extension_days;
ALTER TABLE loan_config DROP COLUMN IF EXISTS extension_fee_percent;
ALTER TABLE loan_config DROP COLUMN IF EXISTS topup_fee_percent;
ALTER TABLE loan_config DROP COLUMN IF EXISTS max_topups;
ALTER TABLE loan_config DROP COLUMN IF EXISTS topup_window_days;
ALTER TABLE loan_config DROP COLUMN IF EXISTS term_days;

COMMIT;
