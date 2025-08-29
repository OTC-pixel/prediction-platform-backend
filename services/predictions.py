#!/usr/bin/env python3
import json
import re
import traceback
from datetime import datetime, timedelta, timezone
from db import get_db

ISO_Z_RE = re.compile(r"Z$")
SCORE_RE = re.compile(r"^\d{1,2}-\d{1,2}$")


def safe_val(r, idx=None, key=None, default=None):
    """
    Safely extract a value from a DB row that may be a tuple/list or dict-like.
    Use idx for tuple/list access, key for dict access.
    """
    if r is None:
        return default
    if isinstance(r, (list, tuple)) and idx is not None:
        try:
            return r[idx]
        except Exception:
            return default
    if isinstance(r, dict) and key is not None:
        return r.get(key, default)
    return default


def _parse_dt(dt):
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        return datetime.fromisoformat(ISO_Z_RE.sub("+00:00", dt))
    raise ValueError("Unsupported datetime format")


def get_latest_completed_matchday():
    """
    Return the last_completed_matchday from matchday_tracker if set and > 0,
    otherwise return the maximum matchday from fixtures that has a result.
    Returns int or None.
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT last_completed_matchday FROM matchday_tracker WHERE id = 1")
        row = cur.fetchone()
        tracker_val = safe_val(row, 0, "last_completed_matchday")
        if tracker_val is not None:
            try:
                val = int(tracker_val)
                if val > 0:
                    return val
            except Exception:
                pass

        cur.execute("SELECT MAX(matchday) AS max_matchday FROM fixtures WHERE result IS NOT NULL")
        row = cur.fetchone()
        latest = safe_val(row, 0, "max_matchday")
        try:
            return int(latest) if latest is not None else None
        except Exception:
            return None
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def submit_matchday_predictions(user_id, predictions):
    """
    predictions: list of {"fixture_id": <int|str>, "predicted_result": "x-y"}
    Returns (True, None) on success or (False, "error msg") on failure.
    """
    if not predictions:
        return False, "No predictions provided"

    try:
        fixture_ids = [int(p["fixture_id"]) for p in predictions]
    except Exception:
        return False, "Invalid fixture_id values"

    if len(set(fixture_ids)) != len(fixture_ids):
        return False, "Duplicate fixture_ids"

    for p in predictions:
        if not SCORE_RE.match(p.get("predicted_result", "")):
            return False, f"Invalid score format for fixture {p.get('fixture_id')}"

    db = get_db()
    cur = db.cursor()
    try:
        # validate user exists
        cur.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
        if cur.fetchone() is None:
            return False, "Invalid user_id"

        # infer matchday from first fixture_id
        cur.execute("SELECT matchday FROM fixtures WHERE fixture_id = %s", (fixture_ids[0],))
        row = cur.fetchone()
        matchday = safe_val(row, 0, "matchday")
        if matchday is None:
            return False, "Invalid fixture_id"

        # fetch provided fixtures and verify they exist
        cur.execute(
            "SELECT fixture_id, kickoff_time, matchday FROM fixtures WHERE fixture_id = ANY(%s)",
            (fixture_ids,),
        )
        rows = cur.fetchall()
        if len(rows) != len(fixture_ids):
            return False, "Unknown fixture_id(s)"

        # ensure all provided fixtures are for the same matchday
        if any(safe_val(r, 2, "matchday") != matchday for r in rows):
            return False, "All predictions must be for one matchday"

        # ensure all fixtures for the matchday are present in submission
        cur.execute("SELECT fixture_id, kickoff_time FROM fixtures WHERE matchday = %s", (matchday,))
        all_rows = cur.fetchall()
        required_ids = {safe_val(r, 0, "fixture_id") for r in all_rows}
        if set(fixture_ids) != required_ids:
            return False, "Must submit ALL fixtures in matchday"

        # check kickoff times: submissions/edits must be before kickoff - 30 minutes
        kickoff_by_id = {}
        for r in all_rows:
            fid = safe_val(r, 0, "fixture_id")
            k = safe_val(r, 1, "kickoff_time")
            try:
                kickoff_by_id[int(fid)] = _parse_dt(k)
            except Exception:
                kickoff_by_id[int(fid)] = None

        now_utc = datetime.now(timezone.utc)
        for p in predictions:
            fid = int(p["fixture_id"])
            kickoff = kickoff_by_id.get(fid)
            if not kickoff or now_utc > kickoff - timedelta(minutes=30):
                return False, f"Submission closed for fixture {p['fixture_id']}"

        # 🔑 Instead of blocking if user already submitted,
        # always insert or update predictions.
        for p in predictions:
            fid, pr = int(p["fixture_id"]), p["predicted_result"]
            cur.execute("SELECT id FROM predictions WHERE user_id = %s AND fixture_id = %s", (user_id, fid))
            row = cur.fetchone()
            if row:
                pred_id = safe_val(row, 0, "id")
                cur.execute(
                    "UPDATE predictions SET predicted_result = %s WHERE id = %s",
                    (pr, pred_id),
                )
            else:
                cur.execute(
                    "INSERT INTO predictions (user_id, fixture_id, predicted_result) VALUES (%s,%s,%s)",
                    (user_id, fid, pr),
                )

        db.commit()
        return True, None
    except Exception as e:
        db.rollback()
        print("Prediction insert error:", e)
        traceback.print_exc()
        return False, "Failed to save predictions"
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def get_user_predictions(user_id):
    """
    Return predictions for the latest matchday (list with one dict) if the user has predictions,
    otherwise return [].
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT MAX(matchday) AS max_matchday FROM fixtures")
        latest_matchday = safe_val(cur.fetchone(), 0, "max_matchday")
        try:
            latest_matchday = int(latest_matchday) if latest_matchday is not None else None
        except Exception:
            latest_matchday = None

        if not latest_matchday:
            return []

        cur.execute("""
            SELECT f.matchday, f.fixture_id, f.home_team, f.away_team, f.kickoff_time,
                   p.predicted_result, p.points_awarded, p.final_result
            FROM fixtures f
            LEFT JOIN predictions p ON f.fixture_id = p.fixture_id AND p.user_id = %s
            WHERE f.matchday = %s
            ORDER BY f.kickoff_time
        """, (user_id, latest_matchday))
        rows = cur.fetchall()
        if not rows:
            return []

        has_predicted = any(safe_val(r, 5, "predicted_result") for r in rows)
        if not has_predicted:
            return []

        fixtures = []
        for r in rows:
            fixtures.append({
                "fixture_id": safe_val(r, 1, "fixture_id"),
                "home_team": safe_val(r, 2, "home_team"),
                "away_team": safe_val(r, 3, "away_team"),
                "kickoff_time": safe_val(r, 4, "kickoff_time"),
                "predicted_result": safe_val(r, 5, "predicted_result"),
                "final_result": safe_val(r, 7, "final_result"),
                "points": safe_val(r, 6, "points_awarded", 0) or 0
            })

        return [{"matchday": latest_matchday, "fixtures": fixtures}]
    except Exception as e:
        print("Error in get_user_predictions:", e)
        traceback.print_exc()
        return []
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def get_predictions_by_matchday(matchday):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT u.username, f.home_team, f.away_team,
                   p.predicted_result, p.points_awarded, p.final_result
            FROM predictions p
            JOIN fixtures f ON p.fixture_id = f.fixture_id
            JOIN users u ON p.user_id = u.id
            WHERE f.matchday = %s
        """, (matchday,))
        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                "username": safe_val(r, 0, "username"),
                "home_team": safe_val(r, 1, "home_team"),
                "away_team": safe_val(r, 2, "away_team"),
                "predicted_result": safe_val(r, 3, "predicted_result"),
                "points": safe_val(r, 4, "points_awarded", 0) or 0,
                "final_result": safe_val(r, 5, "final_result")
            })
        return results
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def update_fixture_result(fixture_id, actual_result):
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE fixtures SET result = %s WHERE fixture_id = %s", (actual_result, fixture_id))
        db.commit()
        return cur.rowcount > 0
    except Exception as e:
        db.rollback()
        print("Fixture update error:", e)
        traceback.print_exc()
        return False
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def evaluate_predictions(fixture_id):
    """
    For a given fixture_id, fetch the actual result from fixtures, compute points,
    and update predictions table with points_awarded and final_result.
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT result FROM fixtures WHERE fixture_id = %s", (fixture_id,))
        actual_result = safe_val(cur.fetchone(), 0, "result")
        if not actual_result:
            return False

        try:
            actual_home, actual_away = map(int, str(actual_result).split("-"))
        except Exception:
            return False

        cur.execute("SELECT id, predicted_result FROM predictions WHERE fixture_id = %s", (fixture_id,))
        predictions = cur.fetchall()

        for pr in predictions:
            prediction_id = safe_val(pr, 0, "id")
            predicted_result = safe_val(pr, 1, "predicted_result")
            points = 0
            if predicted_result:
                try:
                    pred_home, pred_away = map(int, str(predicted_result).split("-"))
                    # exact score
                    if pred_home == actual_home and pred_away == actual_away:
                        points = 5
                    # correct outcome (win/draw/lose)
                    elif ((pred_home - pred_away) * (actual_home - actual_away) > 0) or (pred_home == pred_away and actual_home == actual_away):
                        points = 2
                except Exception:
                    pass
            cur.execute("UPDATE predictions SET points_awarded=%s, final_result=%s WHERE id=%s",
                        (points, actual_result, prediction_id))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print("Prediction evaluation error:", e)
        traceback.print_exc()
        return False
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def process_and_evaluate_latest_matchday():
    """
    Loads stored results for the latest completed matchday, updates fixtures,
    evaluates predictions, records matchday_results and updates leaderboard.
    """
    db = get_db()
    cur = db.cursor()
    try:
        matchday = get_latest_completed_matchday()
        if not matchday:
            print("No matchday ready for processing.")
            return

        cur.execute("SELECT results_json FROM results WHERE matchday = %s", (matchday,))
        row = cur.fetchone()
        if not row:
            print(f"No stored results found for matchday {matchday}.")
            return

        results_json = safe_val(row, 0, "results_json", "[]")
        try:
            results = json.loads(results_json)
        except Exception:
            results = []

        updated_count = 0
        evaluated_count = 0

        for item in results:
            home = item.get("home")
            away = item.get("away")
            kickoff = item.get("kickoff")
            score = item.get("score", {}).get("fulltime", {})
            if score.get("home") is None or score.get("away") is None:
                continue
            result_str = f"{score['home']}-{score['away']}"
            # Update by home/away/kickoff_time; ensure types match DB storage
            cur.execute(
                "UPDATE fixtures SET result = %s WHERE home_team = %s AND away_team = %s AND kickoff_time = %s",
                (result_str, home, away, kickoff))
            if cur.rowcount > 0:
                updated_count += 1

        # Mark remaining null results as explicit 'null' (string) if needed
        cur.execute("UPDATE fixtures SET result = 'null' WHERE matchday = %s AND result IS NULL", (matchday,))
        cancelled_count = cur.rowcount
        db.commit()

        # Evaluate predictions for fixtures in this matchday
        cur.execute("SELECT fixture_id FROM fixtures WHERE matchday = %s", (matchday,))
        fixture_ids = [safe_val(r, 0, "fixture_id") for r in cur.fetchall()]

        for fid in fixture_ids:
            if evaluate_predictions(fid):
                evaluated_count += 1

        print(f"Updated {updated_count} fixture results.")
        print(f"Marked {cancelled_count} matches as cancelled/null.")
        print(f"Evaluated predictions for {evaluated_count} fixtures.")

        # Summarize user points for the matchday
        cur.execute("""
            SELECT p.user_id, SUM(CASE WHEN p.points_awarded IS NOT NULL THEN p.points_awarded ELSE 0 END) AS total_points
            FROM predictions p
            JOIN fixtures f ON p.fixture_id = f.fixture_id
            WHERE f.matchday = %s
            GROUP BY p.user_id
        """, (matchday,))
        user_points = cur.fetchall()

        for up in user_points:
            user_id = safe_val(up, 0, "user_id")
            total_points = safe_val(up, 1, "total_points", 0) or 0
            cur.execute("""
                INSERT INTO matchday_results (matchday, user_id, points)
                VALUES (%s, %s, %s)
                ON CONFLICT (matchday, user_id) DO UPDATE
                SET points = EXCLUDED.points
            """, (matchday, user_id, total_points))

        db.commit()

        # Update leaderboard totals
        for up in user_points:
            user_id = safe_val(up, 0, "user_id")
            cur.execute("SELECT SUM(points) AS total FROM matchday_results WHERE user_id = %s", (user_id,))
            total_points = safe_val(cur.fetchone(), 0, "total", 0) or 0
            cur.execute("""
                INSERT INTO leaderboard (user_id, points, current_matchday, last_updated)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT(user_id) DO UPDATE SET
                    points = EXCLUDED.points,
                    current_matchday = EXCLUDED.current_matchday,
                    last_updated = EXCLUDED.last_updated
            """, (user_id, total_points, matchday))

        db.commit()
        print(f"Leaderboard updated for {len(user_points)} users.")
    except Exception as e:
        db.rollback()
        print("Error processing latest matchday:", e)
        traceback.print_exc()
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def get_final_round_results():
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT MAX(matchday) AS latest_matchday FROM matchday_results")
        latest = safe_val(cur.fetchone(), 0, "latest_matchday")
        try:
            if latest is None:
                return []
            latest = int(latest)
        except Exception:
            return []

        cur.execute("""
            SELECT mr.matchday, u.username, mr.points
            FROM matchday_results mr
            JOIN users u ON mr.user_id = u.id
            WHERE mr.matchday = %s
            ORDER BY mr.points DESC
        """, (latest,))
        rows = cur.fetchall()
        results = []
        for r in rows:
            results.append({
                "matchday": safe_val(r, 0, "matchday"),
                "username": safe_val(r, 1, "username"),
                "points": safe_val(r, 2, "points")
            })
        return results
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def get_user_matchday_performance(user_id, matchday):
    """
    Returns detailed user performance for a given matchday:
    - fixtures with predictions, final results, points
    - total points for the matchday
    - rank among all users for that matchday
    """
    db = get_db()
    cur = db.cursor()
    try:
        # 1️⃣ Fetch fixture-level predictions
        cur.execute("""
            SELECT f.home_team, f.away_team, f.kickoff_time,
                   p.predicted_result, p.final_result, p.points_awarded
            FROM fixtures f
            LEFT JOIN predictions p ON f.fixture_id = p.fixture_id AND p.user_id = %s
            WHERE f.matchday = %s
            ORDER BY f.kickoff_time
        """, (user_id, matchday))
        rows = cur.fetchall()
        if not rows:
            return {"matchday": matchday, "fixtures": [], "total_points": 0, "rank": None}

        fixtures = []
        total_points = 0
        for r in rows:
            points = safe_val(r, 5, "points_awarded", 0) or 0
            total_points += points
            fixtures.append({
                "home_team": safe_val(r, 0, "home_team"),
                "away_team": safe_val(r, 1, "away_team"),
                "kickoff_time": safe_val(r, 2, "kickoff_time"),
                "predicted_result": safe_val(r, 3, "predicted_result"),
                "final_result": safe_val(r, 4, "final_result"),
                "points": points
            })

        # 2️⃣ Fetch ranks for all users for this matchday
        cur.execute("""
            SELECT user_id, points,
                   RANK() OVER (ORDER BY points DESC) AS rank
            FROM matchday_results
            WHERE matchday = %s
        """, (matchday,))
        ranks = cur.fetchall()

        user_rank = None
        for r in ranks:
            if safe_val(r, 0, "user_id") == user_id:
                user_rank = safe_val(r, 2, "rank")
                break

        return {
            "matchday": matchday,
            "fixtures": fixtures,
            "total_points": total_points,
            "rank": user_rank
        }

    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

def get_previous_matchday_performance(user_id):
    """
    Returns performance for the previous matchday
    """
    db = get_db()
    cur = db.cursor()
    try:
        # Convert user_id to integer for consistency
        try:
            user_id_int = int(user_id)
        except (ValueError, TypeError):
            return {"matchday": None, "fixtures": [], "total_points": 0, "rank": "N/A"}

        # 1️⃣ Get latest completed matchday from results table
        cur.execute("SELECT MAX(matchday) AS latest_completed FROM results")
        latest_completed = safe_val(cur.fetchone(), 0, "latest_completed")
        try:
            latest_completed = int(latest_completed) if latest_completed is not None else None
        except Exception:
            latest_completed = None

        # Return empty but properly structured response if no completed matchday
        if not latest_completed:
            return {"matchday": None, "fixtures": [], "total_points": 0, "rank": "N/A"}

        # 2️⃣ Fetch all users' points for that matchday
        cur.execute("""
            SELECT user_id, points
            FROM matchday_results
            WHERE matchday = %s
        """, (latest_completed,))
        all_results = cur.fetchall()

        # Map user_id -> points (ensure user_id is integer)
        user_points_map = {}
        for r in all_results:
            db_user_id = safe_val(r, 0, "user_id")
            if db_user_id is not None:
                try:
                    user_points_map[int(db_user_id)] = safe_val(r, 1, "points", 0) or 0
                except (ValueError, TypeError):
                    continue

        # 3️⃣ Determine user rank using consistent data types
        sorted_users = sorted(user_points_map.items(), key=lambda x: x[1], reverse=True)
        user_rank = "N/A"
        for idx, (uid, pts) in enumerate(sorted_users, start=1):
            if uid == user_id_int:
                user_rank = idx
                break

        # 4️⃣ Get user's fixture-level predictions
        cur.execute("""
            SELECT f.fixture_id, f.home_team, f.away_team, f.kickoff_time,
                   p.predicted_result, p.final_result, p.points_awarded
            FROM fixtures f
            LEFT JOIN predictions p ON f.fixture_id = p.fixture_id AND p.user_id = %s
            WHERE f.matchday = %s
            ORDER BY f.kickoff_time
        """, (user_id_int, latest_completed))
        rows = cur.fetchall()

        fixtures = []
        total_points = 0
        for r in rows:
            points = safe_val(r, 6, "points_awarded", 0) or 0
            total_points += points
            fixtures.append({
                "fixture_id": safe_val(r, 0, "fixture_id"),
                "home_team": safe_val(r, 1, "home_team"),
                "away_team": safe_val(r, 2, "away_team"),
                "kickoff_time": safe_val(r, 3, "kickoff_time"),
                "predicted_result": safe_val(r, 4, "predicted_result"),
                "final_result": safe_val(r, 5, "final_result"),
                "points": points
            })

        # 5️⃣ Return properly structured response
        return {
            "matchday": latest_completed,
            "fixtures": fixtures or [],  # Ensure fixtures is always a list
            "total_points": total_points or 0,
            "rank": user_rank
        }

    except Exception as e:
        print(f"ERROR in get_previous_matchday_performance: {e}")
        # Return properly structured error response
        return {"matchday": None, "fixtures": [], "total_points": 0, "rank": "N/A"}
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


def get_latest_completed_user_predictions(user_id):
    """
    Return the user's performance for the latest completed matchday (fixtures with result IS NOT NULL).
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT MAX(matchday) AS latest_matchday FROM fixtures WHERE result IS NOT NULL")
        latest = safe_val(cur.fetchone(), 0, "latest_matchday")
        try:
            latest = int(latest) if latest is not None else None
        except Exception:
            latest = None

        if not latest:
            return []

        return get_user_matchday_performance(user_id, latest)
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    # When executed directly, process the latest completed matchday.
    process_and_evaluate_latest_matchday()
