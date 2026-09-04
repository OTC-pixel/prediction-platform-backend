import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
import requests
import psycopg2
import psycopg2.extras

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from db import get_db

logger = logging.getLogger(__name__)
from services.predictions import process_and_evaluate_latest_matchday, store_and_evaluate_fixture_result

BBC_API = "https://web-cdn.api.bbci.co.uk/wc-poll-data/container/sport-data-scores-fixtures"
BBC_URN = "urn:bbc:sportsdata:football:tournament-collection:collated"

# How long after kickoff before we bother asking the API whether a fixture
# has finished. 90 min regulation + halftime + typical stoppage time is
# usually done well within 2h; this buffer just avoids wasted API calls
# while a match is still plausibly in progress. Full-time itself is what
# actually gates scoring (via the API's own status field) -- this is only
# "don't bother checking yet", not "trust the clock as the source of truth".
RESULT_CHECK_BUFFER = timedelta(hours=2, minutes=15)


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
        logger.info("Fetching results for %s vs %s on %s...", home, away, date_str)

        params = {
            "selectedStartDate": date_str,
            "selectedEndDate": date_str,
            "todayDate": datetime.now().strftime('%Y-%m-%d'),
            "urn": BBC_URN
        }

        try:
            response = requests.get(BBC_API, params=params)
            if response.status_code != 200:
                logger.warning("Failed request for %s: %s", date_str, response.status_code)
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
            logger.warning("Error fetching results for %s: %s", date_str, e)

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
        logger.info("Stored results and updated records for matchday %s.", matchday)


def run_once():
    """
    Whole-matchday batch path -- kept as the manual/admin fallback (still
    reachable via the CLI / __main__ below, or by calling it directly).
    Not used by the scheduler anymore; see process_pending_results() for
    the automatic per-fixture path.
    """
    matchday = get_latest_completed_matchday()
    if matchday is None:
        logger.info("No completed matchday found or already processed.")
        return
    logger.info("Fetching results for matchday %s...", matchday)
    results_json, human_results = fetch_results_for_matchday(matchday)
    if results_json:
        store_results(matchday, results_json, human_results)
        logger.info("Triggering prediction evaluation...")
        process_and_evaluate_latest_matchday()
        logger.info("Prediction evaluation complete.")
    else:
        logger.info("No valid results found for this matchday.")


# --- Per-fixture incremental path -------------------------------------
#
# Instead of waiting for an entire matchday's last kickoff to be hours in
# the past, this checks each not-yet-scored fixture individually against
# how long ago IT kicked off, and asks the API for that specific date.
# The API already reports each match's status independently (see the
# "status" check in fetch_results_for_matchday above) -- a match doesn't
# need its matchday-mates to be finished for the API to say it's done.

def get_pending_fixtures():
    """
    Fixtures that plausibly have a result available but haven't been
    scored yet: result is still NULL, and kickoff was far enough in the
    past to be worth checking. This is the query that replaces the old
    "wait for the whole matchday" gate -- everything here is judged
    fixture-by-fixture.
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT fixture_id, home_team, away_team, kickoff_time, matchday
            FROM fixtures
            WHERE result IS NULL
              AND kickoff_time::timestamptz <= (NOW() - %s::interval)
            ORDER BY kickoff_time
        """, (f"{int(RESULT_CHECK_BUFFER.total_seconds())} seconds",))
        return cur.fetchall()
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _fetch_finished_events_for_date(date_str):
    """
    One BBC API call for a given date, returning only events that have
    actually gone final (status Result/PostEvent) with a score attached.
    This is the same request/parse logic fetch_results_for_matchday uses
    per-fixture, pulled out so process_pending_results can call it once
    per distinct date instead of once per fixture on that date.
    """
    params = {
        "selectedStartDate": date_str,
        "selectedEndDate": date_str,
        "todayDate": datetime.now().strftime('%Y-%m-%d'),
        "urn": BBC_URN
    }

    finished = []
    try:
        response = requests.get(BBC_API, params=params)
        if response.status_code != 200:
            logger.warning("Failed request for %s: %s", date_str, response.status_code)
            return finished

        data = response.json()
        for group in data.get("eventGroups", []):
            for subgroup in group.get("secondaryGroups", []):
                for event in subgroup.get("events", []):
                    if event.get("status") not in ["Result", "PostEvent"]:
                        continue
                    score_home = event.get("home", {}).get("runningScores", {}).get("fulltime")
                    score_away = event.get("away", {}).get("runningScores", {}).get("fulltime")
                    if score_home is None or score_away is None:
                        continue
                    finished.append({
                        "home": event.get("home", {}).get("fullName", "").lower(),
                        "away": event.get("away", {}).get("fullName", "").lower(),
                        "kickoff": event.get("startDateTime", ""),
                        "home_score": score_home,
                        "away_score": score_away,
                    })
    except Exception as e:
        logger.warning("Error fetching results for %s: %s", date_str, e)

    return finished


def process_pending_results():
    """
    Scheduler entry point for the incremental path: check every
    not-yet-scored fixture whose kickoff is far enough in the past,
    group them by date (one API call per date, not per fixture), and
    score any that the API confirms have finished.

    Safe to run as often as you like -- get_pending_fixtures() only ever
    returns fixtures with result IS NULL, and
    store_and_evaluate_fixture_result() re-checks that same condition
    atomically at write time, so a fixture already scored (by this run or
    a previous one) is never re-fetched or re-processed.
    """
    pending = get_pending_fixtures()
    if not pending:
        logger.info("No pending fixtures to check.")
        return

    by_date = {}
    for f in pending:
        date_str = f["kickoff_time"][:10]
        by_date.setdefault(date_str, []).append(f)

    processed = 0
    for date_str, fixtures in by_date.items():
        logger.info("Checking %s pending fixture(s) for %s...", len(fixtures), date_str)
        events = _fetch_finished_events_for_date(date_str)
        if not events:
            continue

        for fixture in fixtures:
            home = fixture["home_team"].lower()
            away = fixture["away_team"].lower()
            match = next(
                (e for e in events
                 if e["home"] == home and e["away"] == away and e["kickoff"].startswith(date_str)),
                None
            )
            if not match:
                continue

            result_str = f"{match['home_score']}-{match['away_score']}"
            if store_and_evaluate_fixture_result(fixture["fixture_id"], result_str):
                processed += 1
                logger.info(
                    "Scored fixture %s: %s %s %s",
                    fixture["fixture_id"], fixture["home_team"], result_str, fixture["away_team"]
                )

    logger.info("process_pending_results: scored %s fixture(s).", processed)


if __name__ == "__main__":
    run_once()
