import os
import logging
from db import get_db

import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BBC_API_BASE = "https://web-cdn.api.bbci.co.uk/wc-poll-data/container/sport-data-scores-fixtures"

# Ordered preference for Big 8 teams
BIG_EIGHT_ORDER = [
    "Manchester United", "Arsenal", "Liverpool", "Chelsea",
    "Manchester City", "Tottenham Hotspur", "Aston Villa", "Newcastle United"
]


def get_last_kickoff_time():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(kickoff_time) AS max_kickoff FROM fixtures")
    row = cursor.fetchone()

    last_time = row['max_kickoff'] if row and row['max_kickoff'] else None
    return datetime.fromisoformat(last_time) if last_time else None


def fetch_bbc_fixtures_for_day(date_str):
    url = (
        f"{BBC_API_BASE}?selectedStartDate={date_str}"
        f"&selectedEndDate={date_str}"
        f"&todayDate={datetime.today().strftime('%Y-%m-%d')}"
        "&urn=urn%3Abbc%3Asportsdata%3Afootball%3Atournament-collection%3Acollated"
    )

    res = requests.get(url)
    if res.status_code != 200:
        return []

    try:
        data = res.json()
        events = []
        for group in data.get("eventGroups", []):
            if group.get("displayLabel") == "Premier League":  # only EPL
                for sec in group.get("secondaryGroups", []):
                    events.extend(sec.get("events", []))
        return events
    except Exception as e:
        logger.warning("JSON parse error: %s", e)
        return []


def filter_priority_fixtures(events):
    """
    Apply Big 8 preference order. If less than 6 fixtures after filtering,
    fill with other fixtures.
    """
    fixtures = []
    others = []

    for ev in events:
        try:
            home = ev["home"]["fullName"]
            away = ev["away"]["fullName"]
            kickoff = ev["startDateTime"]
            fixture = {"home": home, "away": away, "kickoff": kickoff}
            fixtures.append(fixture)
        except KeyError:
            continue

    # Sort fixtures based on Big 8 preference
    def preference_score(fix):
        teams = [fix["home"], fix["away"]]
        for idx, team in enumerate(BIG_EIGHT_ORDER):
            if team in teams:
                return idx
        return len(BIG_EIGHT_ORDER) + 1  # non Big 8 go last

    fixtures.sort(key=preference_score)

    # Take top 6 preferred fixtures
    selected = fixtures[:6]

    return selected


def try_fetch_fixtures(start_offset, range_days):
    now = datetime.now(timezone.utc)
    collected = []

    for offset in range(start_offset, start_offset + range_days):
        target_date = now + timedelta(days=offset)
        date_str = target_date.strftime('%Y-%m-%d')
        events = fetch_bbc_fixtures_for_day(date_str)
        collected.extend(events)

    # Get only Premier League fixtures
    pl_fixtures = [
        f for f in collected
        if f.get("tournament", {}).get("name") == "Premier League"
    ]

    # Fallback logic: 10 → 9 → 8 fixtures
    if len(pl_fixtures) >= 10:
        return filter_priority_fixtures(pl_fixtures[:10])
    elif len(pl_fixtures) == 9:
        return filter_priority_fixtures(pl_fixtures)
    elif len(pl_fixtures) == 8:
        return filter_priority_fixtures(pl_fixtures)

    return []


def initialize_matchday_tracker():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS count FROM matchday_tracker WHERE id = 1")
    count = cursor.fetchone()["count"]

    if count == 0:
        now_str = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO matchday_tracker (id, current_matchday, last_updated) VALUES (1, 0, %s)",
            (now_str,)
        )
        conn.commit()


def get_next_matchday():
    initialize_matchday_tracker()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT current_matchday FROM matchday_tracker WHERE id = 1")
    row = cursor.fetchone()
    current = row['current_matchday'] if row else 0
    next_matchday = current + 1
    if next_matchday > 38:
        next_matchday = 1

    cursor.execute("UPDATE matchday_tracker SET current_matchday = %s WHERE id = 1", (next_matchday,))
    conn.commit()
    logger.info("Matchday set to: %s", next_matchday)
    return next_matchday


def save_to_db(fixtures, matchday):
    if not fixtures:
        logger.info("No fixtures to save.")
        return

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM fixtures WHERE matchday = %s", (matchday,))
    for idx, fixture in enumerate(fixtures, start=1):
        fixture_id = matchday * 10 + idx
        cursor.execute('''
            INSERT INTO fixtures (fixture_id, matchday, home_team, away_team, kickoff_time)
            VALUES (%s, %s, %s, %s, %s)
        ''', (fixture_id, matchday, fixture["home"], fixture["away"], fixture["kickoff"]))
    conn.commit()
    logger.info("Saved %d fixtures to matchday %s.", len(fixtures), matchday)


def collect_flexible_matchday_fixtures():
    last_kickoff = get_last_kickoff_time()

    if last_kickoff:
        now = datetime.now(timezone.utc)
        first_offset = (last_kickoff + timedelta(days=3) - now).days + 1
        if first_offset < 0:
            first_offset = 0
        fixtures = try_fetch_fixtures(first_offset + 1, 4)
        if fixtures:
            return fixtures

        fixtures = try_fetch_fixtures(1, 16)
        if fixtures:
            return fixtures

    return try_fetch_fixtures(1, 16)


def is_matchday_fully_processed(matchday):
    """
    True only once EVERY fixture in this matchday has a stored result AND
    the resulting points/leaderboard have actually been calculated from
    them. Fixtures are now scored one at a time as they finish (see
    collect_results.py's per-fixture incremental path), so "the round is
    over" can no longer be safely inferred from a fixed number of hours
    since the last kickoff -- a postponed match, a delayed API response,
    or a rescheduled fixture could leave results genuinely incomplete
    well past any fixed timer. This checks the real state instead.
    """
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) AS total, COUNT(result) AS scored FROM fixtures WHERE matchday = %s",
        (matchday,),
    )
    row = cursor.fetchone()
    total = row["total"] if row else 0
    scored = row["scored"] if row else 0
    if total == 0 or scored < total:
        return False

    # Sanity check: outcomes/leaderboard should be calculated for
    # everyone who predicted in this matchday, not just the fixtures
    # themselves marked as scored.
    cursor.execute("""
        SELECT COUNT(DISTINCT p.user_id) AS predictors
        FROM predictions p
        JOIN fixtures f ON p.fixture_id = f.fixture_id
        WHERE f.matchday = %s
    """, (matchday,))
    predictors = cursor.fetchone()["predictors"] or 0

    cursor.execute("SELECT COUNT(*) AS done FROM matchday_results WHERE matchday = %s", (matchday,))
    done = cursor.fetchone()["done"] or 0

    return done >= predictors


def auto_update_if_due():
    initialize_matchday_tracker()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT current_matchday FROM matchday_tracker WHERE id = 1")
    row = cursor.fetchone()
    current_matchday = row["current_matchday"] if row else 0

    # current_matchday == 0 means no matchday has ever been fetched yet
    # (season bootstrap) -- proceed straight to fetching in that case,
    # same as before. Otherwise, only advance once the current matchday
    # is verifiably done: every fixture scored, every predictor's
    # points/leaderboard entry calculated. This replaces the old
    # "14 hours since last kickoff" timer, which assumed the whole round
    # always finished within a fixed window -- true for the old
    # all-at-once batch flow, not guaranteed under incremental per-
    # fixture scoring.
    if current_matchday and current_matchday > 0:
        if not is_matchday_fully_processed(current_matchday):
            logger.info(
                "Matchday %s not fully processed yet (results/leaderboard incomplete) -- "
                "holding off on fetching the next matchday.",
                current_matchday,
            )
            return

    logger.info("Attempting to fetch next matchday fixtures...")
    fixtures = collect_flexible_matchday_fixtures()

    if fixtures:
        matchday = get_next_matchday()
        save_to_db(fixtures, matchday)
        now_str = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE matchday_tracker SET last_updated = %s WHERE id = 1", (now_str,))
        conn.commit()
        logger.info("Matchday %s updated successfully.", matchday)
    else:
        logger.info("No fixtures found. Update aborted.")


if __name__ == "__main__":
    auto_update_if_due()
