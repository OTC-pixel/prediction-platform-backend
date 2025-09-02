from flask import Blueprint, request, jsonify
from services.admin import (
    get_pending_users, approve_user, reject_user,
    add_fixture, get_all_fixtures, get_approved_users,
    delete_user, update_fixture_result, reset_season
)
from dateutil import parser
import pytz
from datetime import datetime

admin_bp = Blueprint('admin', __name__)

# --- Get pending users ---
@admin_bp.route('/pending-users', methods=['GET'])
def pending_users():
    users = get_pending_users()
    return jsonify(users), 200


# --- Approve user ---
@admin_bp.route('/approve-user/<username>', methods=['POST'])
def approve(username):
    clean_username = username.strip()
    success = approve_user(clean_username)
    if success:
        return jsonify({'message': f'{clean_username} approved'}), 200
    return jsonify({'message': 'Approval failed'}), 400


# --- Reject user ---
@admin_bp.route('/reject-user/<username>', methods=['POST'])
def reject(username):
    clean_username = username.strip()
    success = reject_user(clean_username)
    if success:
        return jsonify({'message': f'{clean_username} rejected'}), 200
    return jsonify({'message': 'Rejection failed'}), 400


# --- Add fixture ---
@admin_bp.route('/fixtures', methods=['POST'])
def create_fixture():
    data = request.get_json()
    required_fields = ['matchday', 'home_team', 'away_team', 'kickoff_time']
    if not all(field in data for field in required_fields):
        return jsonify({'message': 'Missing fields'}), 400

    try:
        uk_time = parser.isoparse(data['kickoff_time']).replace(tzinfo=pytz.timezone('Europe/London'))
        utc_time = uk_time.astimezone(pytz.utc)

        success = add_fixture(
            data['matchday'],
            data['home_team'],
            data['away_team'],
            utc_time.isoformat()
        )
        if success:
            return jsonify({'message': 'Fixture added successfully'}), 201
        return jsonify({'message': 'Failed to add fixture'}), 500
    except Exception as e:
        print("Error parsing kickoff_time:", e)
        return jsonify({'message': 'Invalid kickoff_time format'}), 400


# --- List fixtures ---
@admin_bp.route('/fixtures', methods=['GET'])
def list_fixtures():
    try:
        fixtures = get_all_fixtures()
        for fixture in fixtures.get('fixtures', []):
            kickoff = fixture.get('kickoff_time')
            if kickoff:
                fixture['kickoff_time'] = datetime.fromisoformat(kickoff).isoformat()
        return jsonify(fixtures), 200
    except Exception as e:
        print("Error fetching fixtures:", e)
        return jsonify({'message': 'Failed to fetch fixtures'}), 500


# --- Reset season ---
@admin_bp.route('/reset-season', methods=['POST'])
def reset():
    if reset_season():
        return jsonify({'message': 'Season reset successfully'}), 200
    return jsonify({'message': 'Failed to reset season'}), 500


# --- Get approved users ---
@admin_bp.route('/approved-users', methods=['GET'])
def approved_users():
    users = get_approved_users()
    return jsonify(users), 200


# --- Delete user ---
@admin_bp.route('/delete-user/<username>', methods=['DELETE'])
def delete(username):
    clean_username = username.strip()   # ✅ sanitize
    success = delete_user(clean_username)
    if success:
        return jsonify({'message': f'{clean_username} deleted successfully'}), 200
    return jsonify({'message': 'Failed to delete user'}), 400


# --- Post fixture result ---
@admin_bp.route('/results', methods=['POST'])
def post_result():
    data = request.json
    fixture_id = data.get('fixture_id')
    home_score = data.get('home_score')
    away_score = data.get('away_score')

    if fixture_id is None or home_score is None or away_score is None:
        return jsonify({'error': 'Missing data'}), 400

    result_str = f"{home_score}-{away_score}"
    success = update_fixture_result(fixture_id, result_str)

    if success:
        return jsonify({'message': 'Result updated'}), 200
    else:
        return jsonify({'error': 'Failed to update result'}), 500
