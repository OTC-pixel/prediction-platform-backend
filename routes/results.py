from flask import Blueprint, jsonify, request
from db import get_db
import psycopg2.extras

results_bp = Blueprint('results', __name__)

@results_bp.route('/final-round', methods=['GET', 'OPTIONS'])
def final_round_results():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        with get_db() as conn:
            # Use RealDictCursor to get dict-like rows
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Get latest completed matchday
                cur.execute("SELECT MAX(matchday) AS latest_matchday FROM matchday_results")
                latest_row = cur.fetchone()
                if not latest_row or not latest_row["latest_matchday"]:
                    return jsonify([]), 200

                latest_matchday = latest_row["latest_matchday"]

                # Fetch user performance for latest matchday
                cur.execute("""
                    SELECT mr.matchday, u.username, u.full_name, mr.points
                    FROM matchday_results mr
                    JOIN users u ON mr.user_id = u.id
                    WHERE mr.matchday = %s
                    ORDER BY mr.points DESC
                """, (latest_matchday,))
                rows = cur.fetchall()

                return jsonify(rows), 200

    except Exception as e:
        print("Error fetching final round results:", e)
        return jsonify({"error": f"Failed to fetch results: {str(e)}"}), 500
