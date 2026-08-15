import psycopg2
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

def init_db():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    cursor = conn.cursor()

    # USERS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT,
            team TEXT,
            is_approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_treasurer INTEGER DEFAULT 0
        )
    ''')

    # FIXTURES TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fixtures (
            id SERIAL PRIMARY KEY,
            fixture_id INTEGER UNIQUE,
            matchday INTEGER NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_time TEXT NOT NULL,
            result TEXT DEFAULT NULL
        )
    ''')

    # PREDICTIONS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            fixture_id INTEGER,
            predicted_result TEXT,
            points_awarded INTEGER DEFAULT 0,
            final_result TEXT DEFAULT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id)
        )
    ''')

    # MATCHDAY TRACKER TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matchday_tracker (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_matchday INTEGER DEFAULT 0,
            last_completed_matchday INTEGER DEFAULT 0,
            last_updated TEXT
        )
    ''')

    # Insert initial tracker row if not exists
    initial_time = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()
    cursor.execute('''
        INSERT INTO matchday_tracker (id, current_matchday, last_completed_matchday, last_updated)
        VALUES (1, 0, 0, %s)
        ON CONFLICT (id) DO NOTHING
    ''', (initial_time,))

    # RESULTS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS results (
            matchday INTEGER PRIMARY KEY,
            results_json TEXT NOT NULL,
            results_text TEXT DEFAULT NULL,
            updated_at TEXT NOT NULL
        )
    ''')

    # MATCHDAY RESULTS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matchday_results (
            id SERIAL PRIMARY KEY,
            matchday INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            UNIQUE(matchday, user_id)
        )
    ''')

    # LEADERBOARD TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0,
            current_matchday INTEGER DEFAULT 0,
            last_updated TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # MIGRATION: add is_treasurer to pre-existing users tables that were
    # created before this column existed (CREATE TABLE IF NOT EXISTS above
    # won't add it to an already-existing table).
    cursor.execute('''
        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_treasurer INTEGER DEFAULT 0
    ''')

    # ------------------------------------------------------------------
    # Phase 2 -- commitment fee & prediction eligibility
    # ------------------------------------------------------------------

    # Only one row is `active` at a time. Setting a new fee config
    # deactivates the previous one instead of deleting it, so past
    # seasons' fee history survives (matches the append-only-ledger
    # discipline used for the money features generally).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commitment_fee_config (
            id SERIAL PRIMARY KEY,
            amount NUMERIC(12, 2) NOT NULL,
            deadline TIMESTAMPTZ NOT NULL,
            deadline_matchday INTEGER,
            set_by INTEGER REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commitment_fee_status (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            has_paid BOOLEAN NOT NULL DEFAULT FALSE,
            confirmed_by INTEGER REFERENCES users(id),
            confirmed_at TIMESTAMPTZ
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS commitment_fee_exceptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            granted_for_matchday INTEGER NOT NULL,
            granted_by INTEGER REFERENCES users(id),
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')

    # ------------------------------------------------------------------
    # Phase 3 -- savings & surcharge
    # ------------------------------------------------------------------

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_config (
            id SERIAL PRIMARY KEY,
            weekly_minimum NUMERIC(12, 2) NOT NULL,
            surcharge_amount NUMERIC(12, 2) NOT NULL DEFAULT 500,
            set_by INTEGER REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
    ''')

    # Append-only: a confirmed row is never edited. A correction is a new
    # row with reverses_transaction_id pointing back at the original.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount NUMERIC(12, 2) NOT NULL,
            week_start DATE NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL,
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'pending',
            confirmed_by INTEGER REFERENCES users(id),
            confirmed_at TIMESTAMPTZ,
            allocated_savings NUMERIC(12, 2) NOT NULL DEFAULT 0,
            allocated_surcharge NUMERIC(12, 2) NOT NULL DEFAULT 0,
            prioritize_surcharge BOOLEAN NOT NULL DEFAULT FALSE,
            reverses_transaction_id INTEGER REFERENCES savings_transactions(id)
        )
    ''')

    # One surcharge charge per user per missed week. Whether it's cleared
    # is derived from surcharge_clearances, never stored as a flag here.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS surcharge_ledger (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            week_start DATE NOT NULL,
            amount NUMERIC(12, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(user_id, week_start)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS surcharge_clearances (
            id SERIAL PRIMARY KEY,
            surcharge_id INTEGER NOT NULL REFERENCES surcharge_ledger(id),
            savings_transaction_id INTEGER NOT NULL REFERENCES savings_transactions(id),
            amount NUMERIC(12, 2) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')

    # Generic exception-request workflow, scoped to surcharge-priority
    # requests for Phase 3 (commitment-fee exceptions are still granted
    # directly by the Treasurer per Phase 2 -- see services/savings.py
    # module docstring for why these weren't unified).
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS exception_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            type TEXT NOT NULL,
            context TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            decided_by INTEGER REFERENCES users(id),
            decided_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')

    # Singleton tracker so weekly rollover never re-evaluates a week twice.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS savings_tracker (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_processed_week DATE
        )
    ''')
    cursor.execute('''
        INSERT INTO savings_tracker (id, last_processed_week)
        VALUES (1, NULL)
        ON CONFLICT (id) DO NOTHING
    ''')

    # ------------------------------------------------------------------
    # Phase 4 -- loans
    # ------------------------------------------------------------------

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loan_config (
            id SERIAL PRIMARY KEY,
            interest_rate NUMERIC(5, 2) NOT NULL,
            set_by INTEGER REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            active BOOLEAN NOT NULL DEFAULT TRUE
        )
    ''')

    # interest_rate is NULL until disbursement -- it's locked in from
    # whatever loan_config is active at that moment, not at request time.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loans (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            principal NUMERIC(12, 2) NOT NULL,
            interest_rate NUMERIC(5, 2),
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            approved_by INTEGER REFERENCES users(id),
            approved_at TIMESTAMPTZ,
            disbursed_by INTEGER REFERENCES users(id),
            disbursed_at TIMESTAMPTZ,
            rejected_by INTEGER REFERENCES users(id),
            rejected_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loan_repayments (
            id SERIAL PRIMARY KEY,
            loan_id INTEGER NOT NULL REFERENCES loans(id),
            amount NUMERIC(12, 2) NOT NULL,
            idempotency_key TEXT UNIQUE NOT NULL,
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL DEFAULT 'pending',
            confirmed_by INTEGER REFERENCES users(id),
            confirmed_at TIMESTAMPTZ
        )
    ''')

    # ------------------------------------------------------------------
    # Phase 6 -- admin-visible audit log (broader than the public
    # money-rule audit log from Phase 2/3: every admin/treasurer action)
    # ------------------------------------------------------------------

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            actor_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            metadata TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    ''')

    # ------------------------------------------------------------------
    # Phase 5 -- mandatory season-close export, stored in the DB (not on
    # disk) since Render's filesystem is ephemeral and doesn't survive a
    # redeploy/restart. This doubles as the Phase 6 season-end report --
    # one export covers both, since they're the same artifact.
    # ------------------------------------------------------------------

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS season_exports (
            id SERIAL PRIMARY KEY,
            created_by INTEGER REFERENCES users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            csv_content TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database successfully created.")
