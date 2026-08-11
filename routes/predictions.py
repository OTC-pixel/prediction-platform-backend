from flask import Blueprint, request, jsonify
from services.predictions import (
    submit_matchday_predictions, get_user_predictions,
    get_predictions_by_matchday, update_fixture_result,
    evaluate_predictions, process_and_evaluate_latest_matchday,
    get_final_round_results, get_user_matchday_performance,
    get_latest_completed_user_predictions, get_previous_matchday_performance
)
from utils.token import token_required, role_required

predictions_bp = Blueprint("predictions", __name__)


def _forbidden_unless_self_or_admin(target_user_id):
    """
    Any endpoint that returns one user's predictions/performance is gated
    to that user themselves or an admin -- previously anyone with zero
    auth could read any user_id's data (and, worse, submit predictions on
    their behalf). Returns a Flask response to return early, or None if
    the request may proceed.
    """
    requester = getattr(request, "user", {}) or {}
    if requester.get("is_admin"):
        return None
    if requester.get("user_id") == target_user_id:
        return None
    return jsonify({"message": "Forbidden"}), 403


# --- Submit predictions ---
@predictions_bp.route("/submit-matchday-predictions", methods=["POST", "OPTIONS"])
@token_required
def submit_predictions():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    predictions = data.get("predictions")

    # user_id is bound to the authenticated token, never trusted from the
    # request body -- previously a logged-in user could submit predictions
    # for any other user_id just by putting it in the payload.
    user_id = request.user.get("user_id")

    if not user_id or not predictions:
        return jsonify({"message": "Missing user_id or predictions"}), 400
    if not isinstance(predictions, list) or not predictions:
        return jsonify({"message": "Invalid predictions format"}), 400

    try:
        ok, msg = submit_matchday_predictions(user_id, predictions)
        if ok:
            return jsonify({"message": "Predictions submitted"}), 201
        return jsonify({"message": msg or "Submission failed"}), 400
    except Exception as e:
        print("Server error in submit_predictions:", e)
        return jsonify({"message": f"Server error: {str(e)}"}), 500


# --- Get user's predictions ---
@predictions_bp.route("/user-predictions/<int:user_id>", methods=["GET", "OPTIONS"])
@token_required
def user_predictions(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    forbidden = _forbidden_unless_self_or_admin(user_id)
    if forbidden:
        return forbidden
    try:
        result = get_user_predictions(user_id)
        return jsonify(result), 200
    except Exception as e:
        print("Error in user_predictions:", e)
        return jsonify({"error": "Failed to fetch predictions"}), 500


# --- Get predictions by matchday (admin-only: exposes everyone's picks) ---
@predictions_bp.route("/predictions-by-matchday/<int:matchday>", methods=["GET", "OPTIONS"])
@role_required("admin")
def predictions_by_matchday(matchday):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_predictions_by_matchday(matchday)
        return jsonify(result), 200
    except Exception as e:
        print("Error in predictions_by_matchday:", e)
        return jsonify({"error": "Failed to fetch predictions"}), 500


# --- Post fixture result & evaluate (admin only) ---
@predictions_bp.route("/post-result", methods=["POST", "OPTIONS"])
@role_required("admin")
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
            return jsonify({"message": "Result posted and predictions evaluated"}), 200
        return jsonify({"message": "Failed to post result"}), 400
    except Exception as e:
        print("Error in post_result:", e)
        return jsonify({"error": "Failed to post result"}), 500


# --- Admin: process latest matchday ---
@predictions_bp.route("/admin/process-latest-matchday", methods=["POST", "OPTIONS"])
@role_required("admin")
def process_latest_matchday():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        process_and_evaluate_latest_matchday()
        return jsonify({"message": "Processing triggered"}), 200
    except Exception as e:
        print("Error in process_latest_matchday:", e)
        return jsonify({"error": "Failed to process matchday"}), 500


# --- Final round results (public standings, read-only) ---
@predictions_bp.route("/results/final-round", methods=["GET", "OPTIONS"])
def final_round_results():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        result = get_final_round_results()
        return jsonify(result), 200
    except Exception as e:
        print("Error in final_round_results:", e)
        return jsonify({"error": "Failed to fetch results"}), 500


# --- User performance by matchday ---
@predictions_bp.route("/user-matchday-predictions/<int:user_id>/<int:matchday>", methods=["GET", "OPTIONS"])
@token_required
def user_matchday_performance(user_id, matchday):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    forbidden = _forbidden_unless_self_or_admin(user_id)
    if forbidden:
        return forbidden
    try:
        result = get_user_matchday_performance(user_id, matchday)
        return jsonify(result), 200
    except Exception as e:
        print("Error in user_matchday_performance:", e)
        return jsonify({"error": "Failed to fetch performance"}), 500


# --- Latest completed matchday predictions ---
@predictions_bp.route("/user-latest-matchday-predictions/<int:user_id>", methods=["GET", "OPTIONS"])
@token_required
def latest_matchday_predictions(user_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    forbidden = _forbidden_unless_self_or_admin(user_id)
    if forbidden:
        return forbidden
    try:
        result = get_latest_completed_user_predictions(user_id)
        return jsonify(result), 200
    except Exception as e:
        print("Error in latest_matchday_predictions:", e)
        return jsonify({"error": "Failed to fetch latest predictions"}), 500


# --- Previous matchday performance ---
@predictions_bp.route("/user/previous-matchday", methods=["GET", "OPTIONS"])
@token_required
def previous_matchday():
    if request.method == "OPTIONS":
        return jsonify({}), 200

    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    forbidden = _forbidden_unless_self_or_admin(user_id)
    if forbidden:
        return forbidden

    try:
        result = get_previous_matchday_performance(user_id)
        return jsonify(result or {}), 200
    except Exception as e:
        print("Error in previous_matchday:", e)
        return jsonify({"error": "Failed to fetch previous matchday performance"}), 500
