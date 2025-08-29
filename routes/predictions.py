from flask import Blueprint, request, jsonify
from services.predictions import (
    submit_matchday_predictions, get_user_predictions,
    get_predictions_by_matchday, update_fixture_result,
    evaluate_predictions, process_and_evaluate_latest_matchday,
    get_final_round_results, get_user_matchday_performance,
    get_latest_completed_user_predictions, get_previous_matchday_performance
)

predictions_bp = Blueprint("predictions", __name__)


# --- Submit predictions ---
@predictions_bp.route("/submit-matchday-predictions", methods=["POST", "OPTIONS"])
def submit_predictions():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    predictions = data.get("predictions")

    if not user_id or not predictions:
        return jsonify({"message": "Missing user_id or predictions"}), 400
    if not isinstance(predictions, list) or not predictions:
        return jsonify({"message": "Invalid predictions format"}), 400

    try:
        ok, msg = submit_matchday_predictions(user_id, predictions)
        if ok:
            print(f" Predictions submitted for user {user_id}")
            return jsonify({"message": "Predictions submitted"}), 201
        print(f" Prediction submission failed: {msg}")
        return jsonify({"message": msg or "Submission failed"}), 400
    except Exception as e:
        print(" Server error in submit_predictions:", e)
        return jsonify({"message": f"Server error: {str(e)}"}), 500


# --- Get user's predictions ---
@predictions_bp.route("/user-predictions/<int:user_id>", methods=["GET", "OPTIONS"])
def user_predictions(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_user_predictions(user_id)
        print(f" User {user_id} predictions fetched")
        return jsonify(result), 200
    except Exception as e:
        print(" Error in user_predictions:", e)
        return jsonify({"error": "Failed to fetch predictions"}), 500


# --- Get predictions by matchday ---
@predictions_bp.route("/predictions-by-matchday/<int:matchday>", methods=["GET", "OPTIONS"])
def predictions_by_matchday(matchday):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_predictions_by_matchday(matchday)
        print(f" Predictions fetched for matchday {matchday}")
        return jsonify(result), 200
    except Exception as e:
        print(" Error in predictions_by_matchday:", e)
        return jsonify({"error": "Failed to fetch predictions"}), 500


# --- Post fixture result & evaluate ---
@predictions_bp.route("/post-result", methods=["POST", "OPTIONS"])
def post_result():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    fixture_id = data.get("fixture_id")
    actual_result = data.get("actual_result")

    if not fixture_id or actual_result is None:
        return jsonify({"message": "Missing fixture_id or actual_result"}), 400

    try:
        if update_fixture_result(fixture_id, actual_result):
            evaluate_predictions(fixture_id)
            print(f" Result posted for fixture {fixture_id}")
            return jsonify({"message": "Result posted and predictions evaluated"}), 200
        return jsonify({"message": "Failed to post result"}), 400
    except Exception as e:
        print(" Error in post_result:", e)
        return jsonify({"error": "Failed to post result"}), 500


# --- Admin: process latest matchday ---
@predictions_bp.route("/admin/process-latest-matchday", methods=["POST", "OPTIONS"])
def process_latest_matchday():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        process_and_evaluate_latest_matchday()
        print(" Admin triggered latest matchday processing")
        return jsonify({"message": "Processing triggered"}), 200
    except Exception as e:
        print(" Error in process_latest_matchday:", e)
        return jsonify({"error": "Failed to process matchday"}), 500


# --- Final round results ---
@predictions_bp.route("/results/final-round", methods=["GET", "OPTIONS"])
def final_round_results():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_final_round_results()
        return jsonify(result), 200
    except Exception as e:
        print(" Error in final_round_results:", e)
        return jsonify({"error": "Failed to fetch results"}), 500


# --- User performance by matchday ---
@predictions_bp.route("/user-matchday-predictions/<int:user_id>/<int:matchday>", methods=["GET", "OPTIONS"])
def user_matchday_performance(user_id, matchday):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_user_matchday_performance(user_id, matchday)
        return jsonify(result), 200
    except Exception as e:
        print(" Error in user_matchday_performance:", e)
        return jsonify({"error": "Failed to fetch performance"}), 500


# --- Latest completed matchday predictions ---
@predictions_bp.route("/user-latest-matchday-predictions/<int:user_id>", methods=["GET", "OPTIONS"])
def latest_matchday_predictions(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_latest_completed_user_predictions(user_id)
        return jsonify(result), 200
    except Exception as e:
        print(" Error in latest_matchday_predictions:", e)
        return jsonify({"error": "Failed to fetch latest predictions"}), 500


# --- Previous matchday performance ---
@predictions_bp.route("/user/previous-matchday", methods=["GET", "OPTIONS"])
def previous_matchday():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    try:
        result = get_previous_matchday_performance(user_id)
        return jsonify(result or {}), 200
    except Exception as e:
        print(" Error in previous_matchday:", e)
        return jsonify({"error": "Failed to fetch previous matchday performance"}), 500
