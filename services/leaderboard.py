from db import get_db

def get_leaderboard():
    """
    Fetch the leaderboard with points, user info, and current matchday.
    Works safely with RealDictCursor (all rows are dictionaries).
    """
    conn = get_db()
    cursor = conn.cursor()

    # 1️⃣ Get latest completed matchday from fixtures table
    cursor.execute("""
        SELECT MAX(matchday) AS current_matchday
        FROM fixtures
        WHERE result IS NOT NULL
    """)
    latest_row = cursor.fetchone()
    # NOTE: dict.get(key, default) only falls back when the key is absent --
    # here the key is always present, just with SQL NULL (Python None) when
    # no fixture has a result yet (e.g. right after a season reset). `or`
    # correctly falls back on None; the previous code crashed with a
    # None >= int TypeError in that exact state.
    current_matchday = latest_row.get("current_matchday") or 0 if latest_row else 0

    # Determine if the competition is in run-in phase (after matchday 30)
    run_in = current_matchday >= 30

    # 2️⃣ Fetch leaderboard data for approved users
    cursor.execute("""
    SELECT l.points, l.current_matchday, u.username, u.full_name, u.team
    FROM leaderboard l
    JOIN users u ON l.user_id = u.id
    WHERE u.is_approved = 1
    ORDER BY l.points DESC, u.username ASC
""")

    rows = cursor.fetchall()

    # 3️⃣ Build the leaderboard list
    leaderboard = []
    for row in rows:
        leaderboard.append({
            "username": row.get("username", ""),
            "full_name": row.get("full_name", ""),
            "team": row.get("team", ""),
            "points": row.get("points", 0)
        })

    return {
        "current_matchday": current_matchday,
        "run_in": run_in,
        "leaderboard": leaderboard
    }
