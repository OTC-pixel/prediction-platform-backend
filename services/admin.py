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
    username = username.strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            if not user:
                return False
            user_id = user['id']

            cur.execute("DELETE FROM predictions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        print(f"Error rejecting user {username}: {e}")
        return False


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
        cur.execute("SELECT id, username, full_name, team, is_treasurer FROM users WHERE is_approved = 1")
        rows = cur.fetchall()
        return [{
            'id': r['id'],
            'username': r['username'],
            'fullName': r['full_name'],
            'team': r['team'],
            'is_treasurer': bool(r['is_treasurer']),
        } for r in rows]


def delete_user(username):
    username = username.strip()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            if not user:
                return False
            user_id = user['id']

            cur.execute("DELETE FROM predictions WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        print(f"Error deleting user {username}: {e}")
        return False


def update_fixture_result(fixture_id, result):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE fixtures SET result = %s WHERE id = %s", (result, fixture_id))
        conn.commit()
        return cur.rowcount > 0
