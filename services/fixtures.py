from db import get_db

def get_current_matchday_fixtures():
    conn = get_db()
    cursor = conn.cursor()

    # Fetch the current matchday from matchday_tracker
    cursor.execute("SELECT current_matchday FROM matchday_tracker WHERE id = 1")
    row = cursor.fetchone()

    if not row or 'current_matchday' not in row:
        conn.close()
        return None

    current_matchday = row['current_matchday']

    # Fetch fixtures for the current matchday
    cursor.execute("""
        SELECT fixture_id, matchday, home_team, away_team, kickoff_time, result
        FROM fixtures
        WHERE matchday = %s
        ORDER BY kickoff_time ASC
    """, (current_matchday,))
    fixture_rows = cursor.fetchall()

    conn.close()

    fixtures = []
    for row in fixture_rows:
        fixtures.append({
            "fixture_id": row['fixture_id'],
            "matchday": row['matchday'],
            "home_team": row['home_team'],
            "away_team": row['away_team'],
            "kickoff_time": row['kickoff_time'],
            "result": row['result']
        })

    return {
        "matchday": current_matchday,
        "fixtures": fixtures
    }
