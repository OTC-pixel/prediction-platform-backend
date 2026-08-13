from flask import Blueprint, request, jsonify
from services.treasurer import (
    set_fee_config, get_active_fee_config, get_payment_status_list,
    mark_paid, grant_exception, get_exceptions_log, get_user_eligibility,
    get_fee_config_history
)
from utils.token import token_required, role_required
from dateutil import parser
import pytz

treasurer_bp = Blueprint('treasurer', __name__)

LONDON = pytz.timezone('Europe/London')


def _to_utc_iso(dt_str):
    """Same convention as fixture kickoff times: naive input treated as
    Europe/London, then normalized to UTC."""
    naive = parser.isoparse(dt_str).replace(tzinfo=None)
    return LONDON.localize(naive).astimezone(pytz.utc).isoformat()


@treasurer_bp.route('/config', methods=['GET'])
@role_required('admin', 'treasurer')
def get_config():
    config = get_active_fee_config()
    if not config:
        return jsonify(None), 200
    return jsonify({
        'id': config['id'],
        'amount': str(config['amount']),
        'deadline': config['deadline'].isoformat(),
        'deadline_matchday': config['deadline_matchday'],
        'created_at': config['created_at'].isoformat(),
    }), 200


@treasurer_bp.route('/config', methods=['POST'])
@role_required('treasurer')
def post_config():
    data = request.get_json(silent=True) or {}
    amount = data.get('amount')
    deadline = data.get('deadline')
    if amount is None or not deadline:
        return jsonify({'message': 'Missing amount or deadline'}), 400
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid amount'}), 400

    try:
        deadline_utc = _to_utc_iso(deadline)
    except Exception as e:
        print("Error parsing deadline:", e)
        return jsonify({'message': 'Invalid deadline format'}), 400

    set_by = request.user.get('user_id')
    config = set_fee_config(amount, deadline_utc, set_by)
    return jsonify({'message': 'Fee configured', 'id': config['id']}), 201


@treasurer_bp.route('/payment-status', methods=['GET'])
@role_required('admin', 'treasurer')
def payment_status():
    rows = get_payment_status_list()
    return jsonify([
        {
            'user_id': r['id'],
            'username': r['username'],
            'full_name': r['full_name'],
            'has_paid': r['has_paid'],
            'confirmed_at': r['confirmed_at'].isoformat() if r['confirmed_at'] else None,
        }
        for r in rows
    ]), 200


@treasurer_bp.route('/mark-paid/<int:user_id>', methods=['POST'])
@role_required('treasurer')
def post_mark_paid(user_id):
    data = request.get_json(silent=True) or {}
    has_paid = bool(data.get('has_paid', True))
    confirmed_by = request.user.get('user_id')
    mark_paid(user_id, has_paid, confirmed_by)
    return jsonify({'message': 'Payment status updated'}), 200


@treasurer_bp.route('/grant-exception/<int:user_id>', methods=['POST'])
@role_required('treasurer')
def post_grant_exception(user_id):
    granted_by = request.user.get('user_id')
    ok, result = grant_exception(user_id, granted_by)
    if ok:
        return jsonify({'message': 'Exception granted for the current matchday'}), 201
    return jsonify({'message': result}), 400


@treasurer_bp.route('/exceptions', methods=['GET'])
@token_required
def get_exceptions():
    rows = get_exceptions_log()
    return jsonify([
        {
            'id': r['id'],
            'username': r['username'],
            'granted_for_matchday': r['granted_for_matchday'],
            'granted_by': r['granted_by'],
            'granted_at': r['granted_at'].isoformat(),
        }
        for r in rows
    ]), 200


# Public audit log (rebuild plan, Section 2): fee/deadline changes and
# exception grants, visible to every member -- not just admin/treasurer.
@treasurer_bp.route('/audit-log', methods=['GET'])
@token_required
def get_audit_log():
    config_rows = get_fee_config_history()
    exception_rows = get_exceptions_log()

    events = []
    for r in config_rows:
        events.append({
            'type': 'fee_config',
            'amount': str(r['amount']),
            'deadline': r['deadline'].isoformat(),
            'set_by': r['set_by'],
            'at': r['created_at'].isoformat(),
        })
    for r in exception_rows:
        events.append({
            'type': 'exception_grant',
            'username': r['username'],
            'granted_for_matchday': r['granted_for_matchday'],
            'granted_by': r['granted_by'],
            'at': r['granted_at'].isoformat(),
        })

    events.sort(key=lambda e: e['at'], reverse=True)
    return jsonify(events), 200


# Any logged-in user can check their own eligibility -- this is what the
# Predictions page calls to decide whether to show the lock banner.
@treasurer_bp.route('/eligibility', methods=['GET'])
@token_required
def get_eligibility():
    user_id = request.user.get('user_id')
    return jsonify(get_user_eligibility(user_id)), 200
