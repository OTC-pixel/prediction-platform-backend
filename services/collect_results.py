import os
import sys
import json
from datetime import datetime, timezone, timedelta
import requests
import psycopg2
import psycopg2.extras

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from db import get_db
from services.predictions import process_and_evaluate_latest_matchday

BBC_API = "https://web-cdn.api.bbci.co.uk/wc-poll-data/container/sport-data-scores-fixtures"
BBC_URN = "urn:bbc:sportsdata:football:tournament-collection:collated"


# Ensure get_db returns a DictCursor
def get_dict_db():
    conn = get_db()
    conn.cursor_factory = psycopg2.extras.DictCursor
    return conn


def get_latest_completed_matchday():
    """Find the latest matchday whose results have likely been completed."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT DISTINCT matchday FROM fixtures ORDER BY matchday DESC")
            rows = cur.fetchall()

            matchdays = [row['matchday'] for row in rows if row.get('matchday') is not None]

            for md in matchdays:
                cur.execute("SELECT MAX(kickoff_time) AS last_ko FROM fixtures WHERE matchday = %s", (md,))
                row = cur.fetchone()
                last_kickoff = row.get('last_ko') if row else None

                if last_kickoff:
                    if isinstance(last_kickoff, str):
                        last_kickoff_time = datetime.fromisoformat(last_kickoff)
                    else:
                        last_kickoff_time = last_kickoff

                    if datetime.now(timezone.utc) > last_kickoff_time + timedelta(hours=4):
                        cur.execute("SELECT 1 FROM results WHERE matchday = %s", (md,))
                        exists = cur.fetchone()
                        if not exists:
                            return md
    return None


def fetch_results_for_matchday(matchday):
    """Fetch fixture results from the BBC API for a given matchday."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT fixture_id, home_team, away_team, kickoff_time FROM fixtures WHERE matchday = %s", (matchday,))
            fixtures = cur.fetchall()

    results_json = []
    human_results = []

    for fixture in fixtures:
        fixture_id = fixture['fixture_id']
        home = fixture['home_team']
        away = fixture['away_team']
        kickoff = fixture['kickoff_time']

        date_str = kickoff[:10]  # YYYY-MM-DD
        print(f" Fetching results for {home} vs {away} on {date_str}...")

        params = {
            "selectedStartDate": date_str,
            "selectedEndDate": date_str,
            "todayDate": datetime.now().strftime('%Y-%m-%d'),
            "urn": BBC_URN
        }

        try:
            response = requests.get(BBC_API, params=params)
            if response.status_code != 200:
                print(f" Failed request for {date_str}: {response.status_code}")
                continue

            data = response.json()
            event_groups = data.get("eventGroups", [])

            for group in event_groups:
                for subgroup in group.get("secondaryGroups", []):
                    for event in subgroup.get("events", []):
                        if event.get("status") not in ["Result", "PostEvent"]:
                            continue

                        ev_home = event.get("home", {}).get("fullName", "").lower()
                        ev_away = event.get("away", {}).get("fullName", "").lower()
                        ev_kickoff = event.get("startDateTime", "")
                        score_home = event.get("home", {}).get("runningScores", {}).get("fulltime")
                        score_away = event.get("away", {}).get("runningScores", {}).get("fulltime")

                        if (
                            home.lower() == ev_home and
                            away.lower() == ev_away and
                            ev_kickoff.startswith(date_str) and
                            score_home is not None and score_away is not None
                        ):
                            result_str = f"{score_home}-{score_away}"
                            results_json.append({
                                "fixture_id": fixture_id,
                                "home": home,
                                "away": away,
                                "kickoff": kickoff,
                                "score": {
                                    "fulltime": {
                                        "home": score_home,
                                        "away": score_away
                                    }
                                }
                            })
                            human_results.append(f"{home} {score_home} - {score_away} {away}")
                            break
        except Exception as e:
            print(f" Error fetching results for {date_str}: {e}")

    return results_json, human_results


def store_results(matchday, results_json, human_results):
    """Store fetched results in the database and update related tables."""
    now_str = datetime.now(timezone.utc).isoformat()
    results_json_text = json.dumps(results_json)
    human_readable_text = "\n".join(human_results)

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                INSERT INTO results (matchday, results_json, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (matchday) DO UPDATE 
                SET results_json = EXCLUDED.results_json, updated_at = EXCLUDED.updated_at
            """, (matchday, results_json_text, now_str))

            for result in results_json:
                fixture_id = result['fixture_id']
                home_score = result['score']['fulltime']['home']
                away_score = result['score']['fulltime']['away']
                result_str = f"{home_score}-{away_score}"

                cur.execute("UPDATE fixtures SET result = %s WHERE fixture_id = %s", (result_str, fixture_id))
                cur.execute("UPDATE predictions SET final_result = %s WHERE fixture_id = %s", (result_str, fixture_id))

        conn.commit()
        print(f" Stored results and updated records for matchday {matchday}.")


if __name__ == "__main__":
    matchday = get_latest_completed_matchday()
    if matchday is None:
        print(" No completed matchday found or already processed.")
    else:
        print(f" Fetching results for matchday {matchday}...")
        results_json, human_results = fetch_results_for_matchday(matchday)
        if results_json:
            store_results(matchday, results_json, human_results)
            print(" Triggering prediction evaluation...")
            process_and_evaluate_latest_matchday()
            print(" Prediction evaluation complete.")
        else:
            print(" No valid results found for this matchday.")
