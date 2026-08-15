from flask import Blueprint, request, jsonify
from services.loans import (
    set_loan_config, get_active_loan_config, request_loan, endorse_loan,
    get_loans_pending_endorsement, get_loans_pending_approval,
    get_loans_pending_disbursement, approve_loan, reject_loan, disburse_loan,
    submit_repayment, confirm_repayment, reject_repayment, get_pending_repayments,
    get_user_loans, get_all_loans_for_treasurer, get_interest_collected
)
from services.savings import get_surcharge_pool
from services.audit import log_action
from utils.token import token_required, role_required

loans_bp = Blueprint('loans', __name__)


def _serialize_loan(l):
    return {
        'id': l['id'],
        'user_id': l['user_id'],
        'username': l.get('username'),
        'principal': str(l['principal']),
        'interest_rate': str(l['interest_rate']) if l.get('interest_rate') is not None else None,
        'status': l['status'],
        'requested_at': l['requested_at'].isoformat(),
        'approved_at': l['approved_at'].isoformat() if l.get('approved_at') else None,
        'disbursed_at': l['disbursed_at'].isoformat() if l.get('disbursed_at') else None,
        'closed_at': l['closed_at'].isoformat() if l.get('closed_at') else None,
        'endorsement_count': l.get('endorsement_count'),
        'total_owed': str(l['total_owed']) if l.get('total_owed') is not None else None,
        'outstanding': str(l['outstanding']) if l.get('outstanding') is not None else None,
    }


@loans_bp.route('/config', methods=['GET'])
@token_required
def get_config():
    config = get_active_loan_config()
    if not config:
        return jsonify(None), 200
    return jsonify({'interest_rate': str(config['interest_rate'])}), 200


@loans_bp.route('/config', methods=['POST'])
@role_required('treasurer')
def post_config():
    data = request.get_json(silent=True) or {}
    try:
        interest_rate = float(data.get('interest_rate'))
        if interest_rate < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'message': 'Invalid interest_rate'}), 400
    set_loan_config(interest_rate, request.user.get('user_id'))
    return jsonify({'message': 'Interest rate saved'}), 201


@loans_bp.route('/request', methods=['POST'])
@token_required
def post_request():
    data = request.get_json(silent=True) or {}
    user_id = request.user.get('user_id')
    ok, msg, loan = request_loan(user_id, data.get('principal'))
    if ok:
        return jsonify({'message': 'Loan requested', 'id': loan['id']}), 201
    return jsonify({'message': msg}), 400


@loans_bp.route('/pending-endorsement', methods=['GET'])
@token_required
def get_pending_endorsement():
    user_id = request.user.get('user_id')
    rows = get_loans_pending_endorsement(user_id)
    return jsonify([
        {
            'id': r['id'],
            'username': r['username'],
            'principal': str(r['principal']),
            'requested_at': r['requested_at'].isoformat(),
            'endorsement_count': r['endorsement_count'],
            'endorsements_needed': r['endorsements_needed'],
            'already_endorsed': r['already_endorsed'],
        }
        for r in rows
    ]), 200


@loans_bp.route('/<int:loan_id>/endorse', methods=['POST'])
@token_required
def post_endorse(loan_id):
    user_id = request.user.get('user_id')
    ok, msg = endorse_loan(loan_id, user_id)
    if ok:
        return jsonify({'message': 'Endorsement recorded'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/pending-approval', methods=['GET'])
@role_required('treasurer')
def get_pending_approval():
    rows = get_loans_pending_approval()
    return jsonify([_serialize_loan(r) for r in rows]), 200


@loans_bp.route('/pending-disbursement', methods=['GET'])
@role_required('treasurer')
def get_pending_disbursement():
    rows = get_loans_pending_disbursement()
    return jsonify([_serialize_loan(r) for r in rows]), 200


@loans_bp.route('/<int:loan_id>/approve', methods=['POST'])
@role_required('treasurer')
def post_approve(loan_id):
    ok, msg = approve_loan(loan_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_approve', 'loan', loan_id)
        return jsonify({'message': 'Loan approved'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/<int:loan_id>/reject', methods=['POST'])
@role_required('treasurer')
def post_reject(loan_id):
    ok, msg = reject_loan(loan_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_reject', 'loan', loan_id)
        return jsonify({'message': 'Loan rejected'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/<int:loan_id>/disburse', methods=['POST'])
@role_required('treasurer')
def post_disburse(loan_id):
    ok, msg = disburse_loan(loan_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_disburse', 'loan', loan_id)
        return jsonify({'message': 'Loan disbursed'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/<int:loan_id>/repayments', methods=['POST'])
@token_required
def post_repayment(loan_id):
    data = request.get_json(silent=True) or {}
    idempotency_key = data.get('idempotency_key')
    if not idempotency_key:
        return jsonify({'message': 'Missing idempotency_key'}), 400
    user_id = request.user.get('user_id')
    ok, msg, repayment = submit_repayment(loan_id, user_id, data.get('amount'), idempotency_key)
    if ok:
        return jsonify({'message': msg or 'Repayment submitted', 'id': repayment['id']}), 201
    return jsonify({'message': msg}), 400


@loans_bp.route('/repayments/pending', methods=['GET'])
@role_required('treasurer')
def get_repayments_pending():
    rows = get_pending_repayments()
    return jsonify([
        {
            'id': r['id'],
            'loan_id': r['loan_id'],
            'username': r['username'],
            'amount': str(r['amount']),
            'submitted_at': r['submitted_at'].isoformat(),
        }
        for r in rows
    ]), 200


@loans_bp.route('/repayments/<int:repayment_id>/confirm', methods=['POST'])
@role_required('treasurer')
def post_confirm_repayment(repayment_id):
    ok, msg = confirm_repayment(repayment_id, request.user.get('user_id'))
    if ok:
        return jsonify({'message': 'Repayment confirmed'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/repayments/<int:repayment_id>/reject', methods=['POST'])
@role_required('treasurer')
def post_reject_repayment(repayment_id):
    ok, msg = reject_repayment(repayment_id, request.user.get('user_id'))
    if ok:
        return jsonify({'message': 'Repayment rejected'}), 200
    return jsonify({'message': msg}), 400


# Privacy: a member's own loans, or -- if the caller is the Treasurer --
# every member's loans. Nobody else can see another member's loan.
@loans_bp.route('/mine', methods=['GET'])
@token_required
def get_mine():
    user_id = request.user.get('user_id')
    loans = get_user_loans(user_id)
    result = []
    for l in loans:
        serialized = _serialize_loan(l)
        serialized['repayments'] = [
            {
                'id': r['id'],
                'amount': str(r['amount']),
                'status': r['status'],
                'submitted_at': r['submitted_at'].isoformat(),
                'confirmed_at': r['confirmed_at'].isoformat() if r['confirmed_at'] else None,
            }
            for r in l['repayments']
        ]
        result.append(serialized)
    return jsonify(result), 200


@loans_bp.route('/all', methods=['GET'])
@role_required('treasurer')
def get_all():
    rows = get_all_loans_for_treasurer()
    return jsonify([_serialize_loan(r) for r in rows]), 200


# Public: surcharge pool + interest collected, combined into one
# "group fund" figure per the plan (Section 6).
@loans_bp.route('/group-fund', methods=['GET'])
@token_required
def get_group_fund():
    pool = get_surcharge_pool()
    interest = get_interest_collected()
    return jsonify({
        'surcharge_collected': pool['total_collected'],
        'interest_collected': str(interest),
        'group_fund_total': str(float(pool['total_collected']) + float(interest)),
    }), 200
