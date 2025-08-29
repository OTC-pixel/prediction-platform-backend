from db import get_db

def get_latest_completed_matchday():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT MAX(matchday) FROM fixtures WHERE result IS NOT NULL")
    row = cursor.fetchone()
    conn.close()

    return row["max"] if row and row["max"] else None

