from flask import Blueprint, request, jsonify
from services.admin import (
    get_pending_users, approve_user, reject_user,
    add_fixture, get_all_fixtures, get_approved_users,
    delete_user, update_fixture_result
)
from services.treasurer import set_treasurer, set_secretary
from services.audit import log_action
from utils.token import role_required
from dateutil import parser
import pytz
from datetime import datetime

admin_bp = Blueprint('admin', __name__)

# Every route in this file is admin-only. This blueprint used to have zero
# route protection at all -- confirmed live during testing, anyone could
# approve users, add/delete fixtures, or wipe the season with no token.


@admin_bp.route('/pending-users', methods=['GET'])
@role_required('admin')
def pending_users():
    users = get_pending_users()
    return jsonify(users), 200


@admin_bp.route('/approve-user/<username>', methods=['POST'])
@role_required('admin')
def approve(username):
    clean_username = username.strip()
    success = approve_user(clean_username)
    if success:
        log_action(request.user.get('user_id'), 'approve_user', 'user', clean_username)
        return jsonify({'message': f'{clean_username} approved'}), 200
    return jsonify({'message': 'Approval failed'}), 400


@admin_bp.route('/reject-user/<username>', methods=['POST'])
@role_required('admin')
def reject(username):
    clean_username = username.strip()
    success = reject_user(clean_username)
    if success:
        log_action(request.user.get('user_id'), 'reject_user', 'user', clean_username)
        return jsonify({'message': f'{clean_username} rejected'}), 200
    return jsonify({'message': 'Rejection failed'}), 400


@admin_bp.route('/fixtures', methods=['POST'])
@role_required('admin')
def create_fixture():
    data = request.get_json()
    required_fields = ['matchday', 'home_team', 'away_team', 'kickoff_time']
    if not data or not all(field in data for field in required_fields):
        return jsonify({'message': 'Missing fields'}), 400

    try:
        # NOTE: pytz timezones must be applied via localize(), not
        # .replace(tzinfo=...) -- replace() bypasses DST normalization and
        # silently uses London's historical LMT offset (-00:01) instead of
        # the correct BST/GMT offset for the given date.
        london = pytz.timezone('Europe/London')
        naive_time = parser.isoparse(data['kickoff_time']).replace(tzinfo=None)
        uk_time = london.localize(naive_time)
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


@admin_bp.route('/fixtures', methods=['GET'])
@role_required('admin')
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


@admin_bp.route('/approved-users', methods=['GET'])
@role_required('admin')
def approved_users():
    users = get_approved_users()
    return jsonify(users), 200


@admin_bp.route('/delete-user/<username>', methods=['DELETE'])
@role_required('admin')
def delete(username):
    clean_username = username.strip()
    success = delete_user(clean_username)
    if success:
        log_action(request.user.get('user_id'), 'delete_user', 'user', clean_username)
        return jsonify({'message': f'{clean_username} deleted successfully'}), 200
    return jsonify({'message': 'Failed to delete user'}), 400


@admin_bp.route('/set-treasurer/<username>', methods=['POST'])
@role_required('admin')
def set_treasurer_route(username):
    data = request.get_json(silent=True) or {}
    is_treasurer = bool(data.get('is_treasurer', True))
    clean_username = username.strip()
    success = set_treasurer(clean_username, is_treasurer)
    if success:
        verb = 'granted' if is_treasurer else 'revoked'
        log_action(request.user.get('user_id'), f'treasurer_{verb}', 'user', clean_username)
        return jsonify({'message': f'Treasurer role {verb} for {clean_username}'}), 200
    return jsonify({'message': 'User not found'}), 400


@admin_bp.route('/set-secretary/<username>', methods=['POST'])
@role_required('admin')
def set_secretary_route(username):
    data = request.get_json(silent=True) or {}
    is_secretary = bool(data.get('is_secretary', True))
    clean_username = username.strip()
    success = set_secretary(clean_username, is_secretary)
    if success:
        verb = 'granted' if is_secretary else 'revoked'
        log_action(request.user.get('user_id'), f'secretary_{verb}', 'user', clean_username)
        return jsonify({'message': f'Secretary role {verb} for {clean_username}'}), 200
    return jsonify({'message': 'User not found'}), 400


@admin_bp.route('/results', methods=['POST'])
@role_required('admin')
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
