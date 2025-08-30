import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.db_direct import get_direct_connection as get_db

import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

BBC_API_BASE = "https://web-cdn.api.bbci.co.uk/wc-poll-data/container/sport-data-scores-fixtures"

BIG_EIGHT = {
    "Arsenal", "Manchester City", "Manchester United", "Chelsea",
    "Liverpool", "Tottenham Hotspur", "Newcastle United", "Aston Villa"
}

def get_last_kickoff_time():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(kickoff_time) FROM fixtures")
    row = cursor.fetchone()
    conn.close()

    last_time = row[0] if row and row[0] else None
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
        print("JSON parse error:", e)
        return []


def filter_priority_fixtures(events):
    top = []
    others = []

    for ev in events:
        try:
            home = ev["home"]["fullName"]
            away = ev["away"]["fullName"]
            kickoff = ev["startDateTime"]
            fixture = {"home": home, "away": away, "kickoff": kickoff}

            if home in BIG_EIGHT or away in BIG_EIGHT:
                top.append(fixture)
            else:
                others.append(fixture)
        except KeyError:
            continue

    selected = top[:6]
    if len(selected) < 6:
        selected += others[:(6 - len(selected))]

    return selected


def try_fetch_fixtures(start_offset, range_days):
    now = datetime.now(timezone.utc)
    collected = []

    for offset in range(start_offset, start_offset + range_days):
        target_date = now + timedelta(days=offset)
        date_str = target_date.strftime('%Y-%m-%d')
        events = fetch_bbc_fixtures_for_day(date_str)
        collected.extend(events)

        filtered = filter_priority_fixtures(collected)
        if len(filtered) >= 6:
            return filtered[:6]

    return []


def initialize_matchday_tracker():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM matchday_tracker WHERE id = 1")
    count = cursor.fetchone()[0]

    if count == 0:
        now_str = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT INTO matchday_tracker (id, current_matchday, last_updated) VALUES (1, 0, %s)",
            (now_str,)
        )
        conn.commit()
    conn.close()


def get_next_matchday():
    initialize_matchday_tracker()
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT current_matchday FROM matchday_tracker WHERE id = 1")
    row = cursor.fetchone()
    current = row[0] if row else 0
    next_matchday = current + 1
    if next_matchday > 38:
        next_matchday = 1

    cursor.execute("UPDATE matchday_tracker SET current_matchday = %s WHERE id = 1", (next_matchday,))
    conn.commit()
    conn.close()
    print(f"Matchday set to: {next_matchday}")
    return next_matchday


def save_to_db(fixtures, matchday):
    if not fixtures:
        print("No fixtures to save.")
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
    conn.close()
    print(f"Saved {len(fixtures)} fixtures to matchday {matchday}.")


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

        fixtures = try_fetch_fixtures(1, 14)
        if fixtures:
            return fixtures

    return try_fetch_fixtures(1, 7)


def auto_update_if_due():
    initialize_matchday_tracker()
    last_kickoff = get_last_kickoff_time()

    if last_kickoff:
        now = datetime.now(timezone.utc)
        if now < last_kickoff + timedelta(hours=14):  # ⏳ 14 hours instead of 18 or 6
            remaining = (last_kickoff + timedelta(hours=14)) - now
            print(f"{remaining} remaining before update allowed.")
            return

    print("Attempting to fetch next matchday fixtures...")
    fixtures = collect_flexible_matchday_fixtures()

    if fixtures:
        matchday = get_next_matchday()
        save_to_db(fixtures, matchday)
        now_str = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE matchday_tracker SET last_updated = %s WHERE id = 1", (now_str,))
        conn.commit()
        conn.close()
        print(f"Matchday {matchday} updated successfully.")
    else:
        print("No fixtures found. Update aborted.")


if __name__ == "__main__":
    
    auto_update_if_due()

