from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from pytz import timezone as ZoneInfo

from db import get_db
from services.audit import log_action

UK_TIMEZONE = ZoneInfo("Europe/London")
UTC_TIMEZONE = ZoneInfo("UTC")


# ----- User Approval Logic -----
def get_pending_users():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT username, full_name, team FROM users WHERE is_approved = 0")
        rows = cur.fetchall()
        return [{'username': r['username'], 'fullName': r['full_name'], 'team': r['team']} for r in rows]


def approve_user(username):
    username = username.strip()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET is_approved = 1 WHERE username = %s", (username,))
        conn.commit()
        return cur.rowcount > 0


def reject_user(username):
    return _erase_user(username, error_prefix="rejecting")


# ----- Fixture Management -----
def _next_fixture_id(cur, matchday):
    """
    Single ID-generation path for fixtures, whether auto-fetched via the
    scheduler or added manually here. Previously, manually-added fixtures
    never got a fixture_id at all (stayed NULL), breaking anything that
    joins on it (predictions, results). Auto-fetched fixtures use
    `matchday * 10 + index`, so we continue that scheme and just find the
    next free slot in the same matchday block.
    """
    cur.execute(
        "SELECT MAX(fixture_id) AS max_id FROM fixtures WHERE matchday = %s",
        (matchday,)
    )
    row = cur.fetchone()
    max_id = row['max_id'] if row and row['max_id'] else matchday * 10
    return max_id + 1


def add_fixture(matchday, home_team, away_team, kickoff_time_str):
    try:
        local_time = datetime.fromisoformat(kickoff_time_str)
        if local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=UK_TIMEZONE)
        utc_time = local_time.astimezone(UTC_TIMEZONE)
        utc_time_str = utc_time.isoformat()
    except Exception as e:
        print("Datetime conversion error:", e)
        return False

    conn = get_db()
    with conn.cursor() as cur:
        fixture_id = _next_fixture_id(cur, matchday)
        cur.execute("""
            INSERT INTO fixtures (fixture_id, matchday, home_team, away_team, kickoff_time)
            VALUES (%s, %s, %s, %s, %s)
        """, (fixture_id, matchday, home_team, away_team, utc_time_str))
        conn.commit()
        return True


def get_all_fixtures():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, matchday, home_team, away_team, kickoff_time, result "
            "FROM fixtures ORDER BY kickoff_time ASC"
        )
        rows = cur.fetchall()

    fixtures = []
    for row in rows:
        kickoff_time_str = row['kickoff_time']
        try:
            utc_time = datetime.fromisoformat(kickoff_time_str)
            local_time = utc_time.astimezone(UK_TIMEZONE)
            display_time = local_time.isoformat()
        except Exception:
            display_time = kickoff_time_str

        fixtures.append({
            'id': row['id'],
            'matchday': row['matchday'],
            'home_team': row['home_team'],
            'away_team': row['away_team'],
            'kickoff_time': display_time,
            'result': row['result']
        })

    return {'fixtures': fixtures}


def get_approved_users():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, full_name, team, is_treasurer, is_secretary FROM users WHERE is_approved = 1")
        rows = cur.fetchall()
        return [{
            'id': r['id'],
            'username': r['username'],
            'fullName': r['full_name'],
            'team': r['team'],
            'is_treasurer': bool(r['is_treasurer']),
            'is_secretary': bool(r['is_secretary']),
        } for r in rows]


def delete_user(username):
    return _erase_user(username, error_prefix="deleting")


def _erase_user(username, error_prefix="deleting"):
    """
    Fully erase a user and everything that belongs to them. Shared by
    delete_user (approved users) and reject_user (pending users) so both
    stay in sync as the schema grows.

    Two different kinds of FK reference user.id in this schema, and they
    need different treatment:

    1. The user's OWN records (their predictions, savings, loans, etc.)
       -- these are hard-deleted.
    2. Columns where this user acted as admin/treasurer on SOMEONE ELSE'S
       record (confirmed_by, approved_by, granted_by, decided_by, set_by,
       actor_id, disbursed_by, rejected_by, created_by) -- these rows are
       NOT this user's data and must be preserved for the other user's
       financial history / the audit trail. We just null out the
       reference instead of deleting the row.
    """
    username = username.strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            if not user:
                return False
            user_id = user['id']

            # --- 1. Delete the user's own data (children before parents) ---

            # savings_transactions/surcharge_ledger rows this user owns may
            # be referenced by surcharge_clearances; clear those first.
            cur.execute("""
                DELETE FROM surcharge_clearances
                WHERE savings_transaction_id IN (
                    SELECT id FROM savings_transactions WHERE user_id = %s
                )
                OR surcharge_id IN (
                    SELECT id FROM surcharge_ledger WHERE user_id = %s
                )
            """, (user_id, user_id))
            cur.execute("DELETE FROM savings_transactions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM surcharge_ledger WHERE user_id = %s", (user_id,))

            # loan_repayments belong to this user's own loans only.
            cur.execute("""
                DELETE FROM loan_repayments
                WHERE loan_id IN (SELECT id FROM loans WHERE user_id = %s)
            """, (user_id,))
            cur.execute("DELETE FROM loans WHERE user_id = %s", (user_id,))

            cur.execute("DELETE FROM exception_requests WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM commitment_fee_exceptions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM commitment_fee_status WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM leaderboard WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM matchday_results WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM predictions WHERE user_id = %s", (user_id,))

            # --- 2. Scrub this user's identity off OTHER people's records ---
            # (nullable admin/actor columns -- preserve the row, drop the link)
            cur.execute("UPDATE commitment_fee_config SET set_by = NULL WHERE set_by = %s", (user_id,))
            cur.execute("UPDATE commitment_fee_status SET confirmed_by = NULL WHERE confirmed_by = %s", (user_id,))
            cur.execute("UPDATE commitment_fee_exceptions SET granted_by = NULL WHERE granted_by = %s", (user_id,))
            cur.execute("UPDATE savings_config SET set_by = NULL WHERE set_by = %s", (user_id,))
            cur.execute("UPDATE savings_transactions SET confirmed_by = NULL WHERE confirmed_by = %s", (user_id,))
            cur.execute("UPDATE exception_requests SET decided_by = NULL WHERE decided_by = %s", (user_id,))
            cur.execute("UPDATE loan_config SET set_by = NULL WHERE set_by = %s", (user_id,))
            cur.execute("UPDATE loans SET approved_by = NULL WHERE approved_by = %s", (user_id,))
            cur.execute("UPDATE loans SET disbursed_by = NULL WHERE disbursed_by = %s", (user_id,))
            cur.execute("UPDATE loans SET rejected_by = NULL WHERE rejected_by = %s", (user_id,))
            cur.execute("UPDATE loan_repayments SET confirmed_by = NULL WHERE confirmed_by = %s", (user_id,))
            cur.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = %s", (user_id,))
            cur.execute("UPDATE season_exports SET created_by = NULL WHERE created_by = %s", (user_id,))

            # --- 3. Finally, delete the user row itself ---
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        print(f"Error {error_prefix} user {username}: {e}")
        return False


def update_fixture_result(fixture_id, result):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE fixtures SET result = %s WHERE id = %s", (result, fixture_id))
        conn.commit()
        return cur.rowcount > 0