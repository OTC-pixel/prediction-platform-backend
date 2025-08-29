import json
from services.db_direct import get_direct_connection as get_db
from db import get_db
from datetime import datetime, timezone
from services.collect_results import get_latest_completed_matchday


def evaluate_predictions_and_store_totals():
    matchday = get_latest_completed_matchday()
    if not matchday:
        print("🕒 No matchday ready for processing.")
        return

    conn = get_db()
    cursor = conn.cursor()

    # Step 1: Load saved JSON results
    cursor.execute("SELECT results_json FROM results WHERE matchday = %s", (matchday,))
    row = cursor.fetchone()
    if not row:
        print(f"⚠️ No stored results found for matchday {matchday}.")
        conn.close()
        return

    try:
        results = json.loads(row[0])
    except Exception as e:
        print(f"⚠️ Failed to parse results JSON: {e}")
        conn.close()
        return

    updated_count = 0
    evaluated_count = 0

    for item in results:
        home = item['home']
        away = item['away']
        kickoff = item['kickoff']
        score = item.get('score', {}).get('fulltime', {})

        if score.get('home') is None or score.get('away') is None:
            continue

        result_str = f"{score['home']}-{score['away']}"

        # Update fixture result
        cursor.execute("""
            UPDATE fixtures
            SET result = %s
            WHERE home_team = %s AND away_team = %s AND kickoff_time = %s
        """, (result_str, home, away, kickoff))

        cursor.execute("""
            SELECT id FROM fixtures
            WHERE home_team = %s AND away_team = %s AND kickoff_time = %s
        """, (home, away, kickoff))
        fixture_id_row = cursor.fetchone()

        if fixture_id_row:
            fixture_id = fixture_id_row[0]
            cursor.execute("""
                UPDATE predictions
                SET final_result = %s
                WHERE fixture_id = %s
            """, (result_str, fixture_id))
            updated_count += 1

    conn.commit()

    # Step 2: Evaluate predictions for all completed fixtures in this matchday
    cursor.execute("SELECT id FROM fixtures WHERE matchday = %s AND result IS NOT NULL", (matchday,))
    fixture_ids = [row[0] for row in cursor.fetchall()]

    for fixture_id in fixture_ids:
        cursor.execute("SELECT result FROM fixtures WHERE id = %s", (fixture_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            continue

        actual_result = row[0]
        try:
            actual_home, actual_away = map(int, actual_result.split("-"))
        except:
            continue

        cursor.execute("SELECT id, predicted_result FROM predictions WHERE fixture_id = %s", (fixture_id,))
        predictions = cursor.fetchall()

        for prediction_id, predicted_result in predictions:
            if not predicted_result:
                points = 0
            else:
                try:
                    pred_home, pred_away = map(int, predicted_result.split("-"))
                    if pred_home == actual_home and pred_away == actual_away:
                        points = 5
                    elif (pred_home - pred_away) * (actual_home - actual_away) > 0 or (pred_home == pred_away and actual_home == actual_away):
                        points = 2
                    else:
                        points = 0
                except:
                    points = 0

            cursor.execute("UPDATE predictions SET points_awarded = %s WHERE id = %s", (points, prediction_id))

        evaluated_count += 1

    conn.commit()

    # Step 3: Save total points per user in matchday_results
    cursor.execute("DELETE FROM matchday_results WHERE matchday = %s", (matchday,))

    cursor.execute("""
        INSERT INTO matchday_results (matchday, user_id, points)
        SELECT f.matchday, p.user_id, SUM(p.points_awarded)
        FROM predictions p
        JOIN fixtures f ON p.fixture_id = f.id
        WHERE f.matchday = %s
        GROUP BY p.user_id
    """, (matchday,))

    conn.commit()
    conn.close()

    print(f"✅ {updated_count} fixture results updated.")
    print(f"📊 {evaluated_count} fixtures evaluated.")
    print(f"🗒️ Total points stored for matchday {matchday}.")


if __name__ == "__main__":
    evaluate_predictions_and_store_totals()
