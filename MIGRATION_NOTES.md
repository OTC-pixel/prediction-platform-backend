# Loan engine: migration notes

## Deploy order (live site)
1. **Back up the database** (Supabase: Database > Backups, or `pg_dump`).
2. **Run the SQL**: `database/migrations/2026_11_loan_engine.sql`
   - Supabase: SQL Editor > New query > paste the whole file > Run.
   - Or: `psql "$DATABASE_URL" -f database/migrations/2026_11_loan_engine.sql`
   - Safe to run more than once. One transaction: all or nothing.
   - `database/init_db.py` makes the same changes for fresh databases.
3. **Deploy this backend** (no new Python packages; `requirements.txt` is unchanged).
4. **Deploy the frontend** (`frontend.zip`).
5. As Treasurer: Treasurer page > Loan rules. Your current rate carries over as the fee;
   term, top-ups, extension and late fee start at 9%-style defaults. Set a "starting loan
   limit" there only if you want limits to grow with good repayment (blank = off).

## What happens to existing data
- Loans already out keep their fee and get a **fresh 30-day term from the moment you run the SQL**
  (so nobody is charged a late fee because of the upgrade). Balances are unchanged.
- Pending / endorsed / approved requests and all savings are untouched.
- Late fees are applied by a job that runs at backend start-up and then hourly.
- `loan_endorsements` is created if it is missing (it never was in `init_db.py`).

## Rolling back
`database/migrations/2026_11_loan_engine_ROLLBACK.sql` removes the additions again. It deletes
the fee/top-up ledger and due dates recorded since, so use it only together with the old backend.

## Files changed in this zip
Added:    database/migrations/2026_11_loan_engine.sql, database/migrations/2026_11_loan_engine_ROLLBACK.sql
Modified: database/init_db.py, routes/loans.py, routes/savings.py, scheduler.py,
          services/inbox.py, services/loans.py, services/season_close.py
Everything else is exactly as you sent it.
(Your `.git` folder is included, so `git status` / `git diff` shows precisely what changed.)

## Known gap
Season close still deletes any loans that are open at the reset. Settle or carry over open loans
first, or ask for season-close handling to be added.
