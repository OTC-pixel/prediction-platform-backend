from flask import Blueprint, request, jsonify
from services.savings import (
    set_savings_config, get_active_savings_config, submit_transaction,
    confirm_transaction, reject_transaction, get_pending_transactions,
    process_week_rollover, request_exception, decide_exception_request,
    get_exception_requests, get_user_ledger, get_surcharge_pool,
    get_total_savings_balance, get_members_savings_overview
)
from utils.token import token_required, role_required

savings_bp = Blueprint('savings', __name__)


@savings_bp.route('/config', methods=['GET'])
@token_required
def get_config():
    config = get_active_savings_config()
    if not config:
        return jsonify(None), 200
    return jsonify({
        'id': config['id'],
        'weekly_minimum': str(config['weekly_minimum']),
        'surcharge_amount': str(config['surcharge_amount']),
        'created_at': config['created_at'].isoformat(),
    }), 200


@savings_bp.route('/config', methods=['POST'])
@role_required('treasurer')
def post_config():
    data = request.get_json(silent=True) or {}
    try:
        weekly_minimum = float(data.get('weekly_minimum'))
        surcharge_amount = float(data.get('surcharge_amount'))
        if weekly_minimum <= 0 or surcharge_amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid weekly_minimum or surcharge_amount'}), 400

    set_by = request.user.get('user_id')
    config = set_savings_config(weekly_minimum, surcharge_amount, set_by)
    return jsonify({'message': 'Savings config updated', 'id': config['id']}), 201


@savings_bp.route('/transactions', methods=['POST'])
@token_required
def post_transaction():
    data = request.get_json(silent=True) or {}
    amount = data.get('amount')
    idempotency_key = data.get('idempotency_key')
    if not idempotency_key:
        return jsonify({'message': 'Missing idempotency_key'}), 400

    user_id = request.user.get('user_id')
    ok, msg, txn = submit_transaction(user_id, amount, idempotency_key)
    if ok:
        return jsonify({'message': msg or 'Transaction submitted', 'id': txn['id']}), 201
    return jsonify({'message': msg}), 400


@savings_bp.route('/transactions/mine', methods=['GET'])
@token_required
def get_my_ledger():
    user_id = request.user.get('user_id')
    return jsonify(get_user_ledger(user_id)), 200


@savings_bp.route('/transactions/pending', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_pending():
    rows = get_pending_transactions()
    return jsonify([
        {
            'id': r['id'],
            'username': r['username'],
            'user_id': r['user_id'],
            'amount': str(r['amount']),
            'week_start': r['week_start'].isoformat(),
            'submitted_at': r['submitted_at'].isoformat(),
        }
        for r in rows
    ]), 200


@savings_bp.route('/transactions/<int:transaction_id>/confirm', methods=['POST'])
@role_required('treasurer')
def post_confirm(transaction_id):
    data = request.get_json(silent=True) or {}
    prioritize_surcharge = bool(data.get('prioritize_surcharge', False))
    confirmed_by = request.user.get('user_id')
    ok, msg = confirm_transaction(transaction_id, confirmed_by, prioritize_surcharge)
    if ok:
        return jsonify({'message': 'Transaction confirmed'}), 200
    return jsonify({'message': msg}), 400


@savings_bp.route('/transactions/<int:transaction_id>/reject', methods=['POST'])
@role_required('treasurer')
def post_reject(transaction_id):
    confirmed_by = request.user.get('user_id')
    ok, msg = reject_transaction(transaction_id, confirmed_by)
    if ok:
        return jsonify({'message': 'Transaction rejected'}), 200
    return jsonify({'message': msg}), 400


@savings_bp.route('/process-week-rollover', methods=['POST'])
@role_required('admin', 'treasurer')
def post_rollover():
    process_week_rollover()
    return jsonify({'message': 'Week rollover processed'}), 200


@savings_bp.route('/surcharge-pool', methods=['GET'])
@token_required
def get_pool():
    return jsonify(get_surcharge_pool()), 200


# ---------- Treasurer / Secretary cash-reconciliation dashboard ----------

@savings_bp.route('/members-overview', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_members_overview():
    rows = get_members_savings_overview()
    return jsonify([
        {
            'user_id': r['user_id'],
            'username': r['username'],
            'full_name': r['full_name'],
            'savings_balance': str(r['savings_balance']),
            'surcharge_owed': str(r['surcharge_owed']),
        }
        for r in rows
    ]), 200


@savings_bp.route('/members/<int:user_id>/ledger', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_member_ledger(user_id):
    ledger = get_user_ledger(user_id)
    return jsonify(ledger), 200


@savings_bp.route('/wallet-summary', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_wallet_summary():
    total_savings = get_total_savings_balance()
    pool = get_surcharge_pool()
    return jsonify({
        'total_savings_balance': str(total_savings),
        'surcharge_total_collected': str(pool['total_collected']),
        'surcharge_total_owed': str(pool['total_owed']),
        'surcharge_total_expected': str(pool['total_charged']),
    }), 200


@savings_bp.route('/exception-requests', methods=['POST'])
@token_required
def post_exception_request():
    data = request.get_json(silent=True) or {}
    context = (data.get('context') or '').strip()
    user_id = request.user.get('user_id')
    row = request_exception(user_id, 'surcharge_priority', context)
    return jsonify({'message': 'Exception requested', 'id': row['id']}), 201


@savings_bp.route('/exception-requests', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_exceptions():
    status = request.args.get('status')
    rows = get_exception_requests(status)
    return jsonify([
        {
            'id': r['id'],
            'username': r['username'],
            'type': r['type'],
            'context': r['context'],
            'status': r['status'],
            'created_at': r['created_at'].isoformat(),
        }
        for r in rows
    ]), 200


@savings_bp.route('/exception-requests/<int:request_id>/decide', methods=['POST'])
@role_required('treasurer')
def post_decide(request_id):
    data = request.get_json(silent=True) or {}
    approve = bool(data.get('approve'))
    decided_by = request.user.get('user_id')
    ok, msg = decide_exception_request(request_id, approve, decided_by)
    if ok:
        return jsonify({'message': 'Decision recorded'}), 200
    return jsonify({'message': msg}), 400
