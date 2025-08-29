from flask import Blueprint, jsonify, request
from services.leaderboard import get_leaderboard as fetch_leaderboard

leaderboard_bp = Blueprint('leaderboard', __name__)

@leaderboard_bp.route('/', methods=['GET', 'OPTIONS'])
def leaderboard():
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({}), 200

    try:
        data = fetch_leaderboard()
        return jsonify(data), 200
    except Exception as e:
        print("Error fetching leaderboard:", e)
        return jsonify({"error": "Failed to fetch leaderboard"}), 500
