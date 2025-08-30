import psycopg2
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from pytz import timezone as ZoneInfo

# Load DB credentials from .env
load_dotenv()

DB_PARAMS = {
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT'),
    'dbname': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD')
}

UK_TIMEZONE = ZoneInfo("Europe/London")
UTC_TIMEZONE = ZoneInfo("UTC")

def get_connection():
    return psycopg2.connect(**DB_PARAMS)


# ----- User Approval Logic -----
def get_pending_users():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username, full_name, team FROM users WHERE is_approved = 0")
            rows = cur.fetchall()
            return [{'username': r[0], 'fullName': r[1], 'team': r[2]} for r in rows]

def approve_user(username):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_approved = 1 WHERE username = %s", (username,))
            conn.commit()
            return cur.rowcount > 0

def reject_user(username):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1. First get user ID
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            
            if not user:
                return False
                
            user_id = user[0]
            
            # 2. Delete user's predictions first
            cur.execute("DELETE FROM predictions WHERE user_id = %s", (user_id,))
            
            # 3. Now delete the user
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        print(f"Error rejecting user {username}: {e}")
        return False
    finally:
        conn.close()


# ----- Fixture Management -----
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

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fixtures (matchday, home_team, away_team, kickoff_time)
                VALUES (%s, %s, %s, %s)
            """, (matchday, home_team, away_team, utc_time_str))
            conn.commit()
            return True

def get_all_fixtures():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, matchday, home_team, away_team, kickoff_time, result FROM fixtures ORDER BY kickoff_time ASC")
            rows = cur.fetchall()

    fixtures = []
    for row in rows:
        kickoff_time_str = row[4]
        try:
            utc_time = datetime.fromisoformat(kickoff_time_str)
            local_time = utc_time.astimezone(UK_TIMEZONE)
            display_time = local_time.isoformat()
        except Exception:
            display_time = kickoff_time_str

        fixtures.append({
            'id': row[0],
            'matchday': row[1],
            'home_team': row[2],
            'away_team': row[3],
            'kickoff_time': display_time,
            'result': row[5]
        })

    return {'fixtures': fixtures}


def reset_season():
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM predictions")
                cur.execute("DELETE FROM fixtures")
                cur.execute("DELETE FROM results")
                cur.execute("DELETE FROM matchday_results")
                cur.execute("DELETE FROM leaderboard")
                cur.execute("""
                    UPDATE matchday_tracker
                    SET current_matchday = 0, last_completed_matchday = 0, last_updated = %s
                    WHERE id = 1
                """, (now_str,))
                conn.commit()
                print("Season reset complete.")
                return True
    except Exception as e:
        print(f"Reset failed: {e}")
        return False


def get_approved_users():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, full_name, team FROM users WHERE is_approved = 1")
            rows = cur.fetchall()
            return [{'id': r[0], 'username': r[1], 'full_name': r[2], 'team': r[3]} for r in rows]


def delete_user(username):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1. First get user ID
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            user = cur.fetchone()
            
            if not user:
                return False
                
            user_id = user[0]
            
            # 2. Delete user's predictions first
            cur.execute("DELETE FROM predictions WHERE user_id = %s", (user_id,))
            
            # 3. Now delete the user
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        print(f"Error deleting user {username}: {e}")
        return False
    finally:
        conn.close()


def update_fixture_result(fixture_id, result):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fixtures SET result = %s WHERE id = %s", (result, fixture_id))
            conn.commit()
            return cur.rowcount > 0