from flask import Blueprint, request, jsonify
from services.loans import (
    set_loan_config, get_active_loan_config, effective_rules, request_loan, endorse_loan,
    get_loans_pending_endorsement, get_loans_pending_approval,
    get_loans_pending_disbursement, approve_loan, reject_loan, disburse_loan,
    submit_repayment, confirm_repayment, reject_repayment, get_pending_repayments,
    get_user_loans, get_all_loans_for_treasurer, get_interest_collected,
    get_borrowing_limit, get_portfolio, request_topup, approve_topup, reject_topup,
    extend_loan, get_loan_signal,
)
from services.savings import get_surcharge_pool
from services.audit import log_action
from utils.token import token_required, role_required

loans_bp = Blueprint('loans', __name__)


def _s(v):
    return str(v) if v is not None else None


def _iso(v):
    return v.isoformat() if v else None


def _charge(c):
    return {'id': c['id'], 'kind': c['kind'], 'amount': str(c['amount']), 'note': c.get('note'),
            'created_at': _iso(c['created_at'])}


def _repayment(r):
    return {
        'id': r['id'],
        'amount': str(r['amount']),
        'status': r['status'],
        'submitted_at': _iso(r['submitted_at']),
        'confirmed_at': _iso(r['confirmed_at']),
    }


def _serialize_loan(l):
    out = {
        'id': l['id'],
        'user_id': l['user_id'],
        'username': l.get('username'),
        'name': l.get('full_name') or l.get('username'),
        'principal': str(l['principal']),
        'interest_rate': _s(l.get('interest_rate')),
        'status': l['status'],
        'requested_at': _iso(l['requested_at']),
        'approved_at': _iso(l.get('approved_at')),
        'disbursed_at': _iso(l.get('disbursed_at')),
        'closed_at': _iso(l.get('closed_at')),
        'endorsement_count': l.get('endorsement_count'),
        'total_owed': _s(l.get('total_owed')),
        'outstanding': _s(l.get('outstanding')),
    }
    # Present only for a loan that is out (see services.loans._serialize_state)
    if 'standing' in l:
        out.update({
            'principal_total': _s(l['principal_total']),
            'fee_total': _s(l['fee_total']),
            'repaid': _s(l['repaid']),
            'awaiting_confirmation': _s(l['awaiting_confirmation']),
            'due_at': _iso(l['due_at']),
            'days_spent': l['days_spent'],
            'days_left': l['days_left'],
            'days_overdue': l['days_overdue'],
            'standing': l['standing'],
            'topups_used': l['topups_used'],
            'extensions_used': l['extensions_used'],
            'charges': [_charge(c) for c in l['charges']],
        })
    return out


# ---------------------------------------------------------------- config

CONFIG_NUMBER_FIELDS = {
    # name: (type, minimum, maximum, optional)
    'interest_rate': (float, 0, 100, False),
    'term_days': (int, 1, 365, True),
    'topup_window_days': (int, 0, 365, True),
    'max_topups': (int, 0, 20, True),
    'topup_fee_percent': (float, 0, 100, True),
    'extension_fee_percent': (float, 0, 100, True),
    'extension_days': (int, 1, 365, True),
    'max_extensions': (int, 0, 20, True),
    'late_fee_percent': (float, 0, 100, True),
    'default_after_days': (int, 1, 365, True),
    'starter_limit': (float, 0, 1_000_000_000, True),
    'limit_step_percent': (float, 0, 1000, True),
}
# Blank means "follow the main fee" for these; blank starter_limit means off.
NULLABLE_FIELDS = {'topup_fee_percent', 'extension_fee_percent', 'late_fee_percent', 'starter_limit'}


def _config_json(config):
    if not config:
        return None
    rules = effective_rules(config)
    return {
        'interest_rate': str(config['interest_rate']),
        'term_days': config['term_days'],
        'topup_window_days': config['topup_window_days'],
        'max_topups': config['max_topups'],
        'topup_fee_percent': _s(config['topup_fee_percent']),
        'extension_fee_percent': _s(config['extension_fee_percent']),
        'extension_days': config['extension_days'],
        'max_extensions': config['max_extensions'],
        'late_fee_percent': _s(config['late_fee_percent']),
        'default_after_days': config['default_after_days'],
        'starter_limit': _s(config['starter_limit']),
        'limit_step_percent': str(config['limit_step_percent']),
        # what a loan paid out today would actually carry
        'effective': rules,
    }


@loans_bp.route('/config', methods=['GET'])
@token_required
def get_config():
    return jsonify(_config_json(get_active_loan_config())), 200


@loans_bp.route('/config', methods=['POST'])
@role_required('treasurer')
def post_config():
    data = request.get_json(silent=True) or {}
    values = {}
    for name, (kind, lo, hi, optional) in CONFIG_NUMBER_FIELDS.items():
        raw = data.get(name)
        if raw is None or raw == '':
            if not optional:
                return jsonify({'message': f'Invalid {name}'}), 400
            if name in NULLABLE_FIELDS:
                values[name] = None   # explicit blank: follow the main fee / tier off
            continue                  # otherwise: column default
        try:
            v = kind(raw) if kind is float else int(float(raw))
        except (TypeError, ValueError):
            return jsonify({'message': f'Invalid {name}'}), 400
        if not (lo <= v <= hi):
            return jsonify({'message': f'{name} must be between {lo} and {hi}'}), 400
        values[name] = v
    if values.get('starter_limit') == 0:
        values['starter_limit'] = None
    set_loan_config(values, request.user.get('user_id'))
    log_action(request.user.get('user_id'), 'loan_config', 'loan_config', None)
    return jsonify({'message': 'Loan rules saved'}), 201


# --------------------------------------------------------------- member

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
            'name': r.get('full_name') or r['username'],
            'principal': str(r['principal']),
            'requested_at': _iso(r['requested_at']),
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


@loans_bp.route('/<int:loan_id>/topups', methods=['POST'])
@token_required
def post_topup(loan_id):
    data = request.get_json(silent=True) or {}
    ok, msg, topup = request_topup(loan_id, request.user.get('user_id'), data.get('amount'))
    if ok:
        return jsonify({'message': 'Top-up requested', 'id': topup['id']}), 201
    return jsonify({'message': msg}), 400


@loans_bp.route('/<int:loan_id>/extend', methods=['POST'])
@token_required
def post_extend(loan_id):
    ok, msg, result = extend_loan(loan_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_extend', 'loan', loan_id)
        return jsonify({'message': 'Loan extended', 'fee': str(result['fee']), 'due_at': _iso(result['due_at'])}), 200
    return jsonify({'message': msg}), 400


# Privacy: a member's own loans. Officers see everyone's via /portfolio.
@loans_bp.route('/mine', methods=['GET'])
@token_required
def get_mine():
    user_id = request.user.get('user_id')
    result = []
    for l in get_user_loans(user_id):
        s = _serialize_loan(l)
        s['repayments'] = [_repayment(r) for r in l['repayments']]
        if l['status'] == 'disbursed':
            s.update({
                'can_topup': l['can_topup'],
                'topup_block_reason': l['topup_block_reason'],
                'topup_available': str(l['topup_available']),
                'topup_fee_percent': l['topup_fee_percent'],
                'topups_left': l['topups_left'],
                'can_extend': l['can_extend'],
                'extend_block_reason': l['extend_block_reason'],
                'extension_fee': str(l['extension_fee']),
                'extension_days': l['extension_days'],
                'extensions_left': l['extensions_left'],
                'topups': [{
                    'id': t['id'], 'amount': str(t['amount']), 'status': t['status'],
                    'requested_at': _iso(t['requested_at']),
                } for t in l['topups']],
            })
        result.append(s)
    return jsonify(result), 200


# What this member may borrow right now (shown before they type a number).
@loans_bp.route('/limit', methods=['GET'])
@token_required
def get_limit():
    lim = get_borrowing_limit(request.user.get('user_id'))
    return jsonify({
        'savings_balance': str(lim['savings_balance']),
        'cap_percent': lim['cap_percent'],
        'savings_cap': lim['savings_cap'],
        'tier_limit': lim['tier_limit'],
        'tier_level': lim['tier_level'],
        'on_time_repaid': lim['on_time_repaid'],
        'late_repaid': lim['late_repaid'],
        'limit': lim['limit'],
        'used': str(lim['used']),
        'available': str(lim['available']),
        'standing': lim['standing'],
        'max_request': lim['max_request'],
    }), 200


# -------------------------------------------------------------- officers

@loans_bp.route('/pending-approval', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_pending_approval():
    return jsonify([_serialize_loan(r) for r in get_loans_pending_approval()]), 200


@loans_bp.route('/pending-disbursement', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_pending_disbursement():
    return jsonify([_serialize_loan(r) for r in get_loans_pending_disbursement()]), 200


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


@loans_bp.route('/repayments/pending', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_repayments_pending():
    rows = get_pending_repayments()
    return jsonify([
        {
            'id': r['id'],
            'loan_id': r['loan_id'],
            'username': r['username'],
            'name': r.get('full_name') or r['username'],
            'amount': str(r['amount']),
            'submitted_at': _iso(r['submitted_at']),
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


@loans_bp.route('/topups/<int:topup_id>/approve', methods=['POST'])
@role_required('treasurer')
def post_approve_topup(topup_id):
    ok, msg = approve_topup(topup_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_topup_approve', 'loan_topup', topup_id)
        return jsonify({'message': 'Top-up approved'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/topups/<int:topup_id>/reject', methods=['POST'])
@role_required('treasurer')
def post_reject_topup(topup_id):
    ok, msg = reject_topup(topup_id, request.user.get('user_id'))
    if ok:
        log_action(request.user.get('user_id'), 'loan_topup_reject', 'loan_topup', topup_id)
        return jsonify({'message': 'Top-up rejected'}), 200
    return jsonify({'message': msg}), 400


@loans_bp.route('/all', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_all():
    return jsonify([_serialize_loan(r) for r in get_all_loans_for_treasurer()]), 200


# One call for the officer view of the Loans page (Treasurer, Secretary,
# Admin): loans currently out with balances, due dates and lateness, the
# work queues, aging buckets and headline totals. Only the Treasurer can
# act on the queues.
@loans_bp.route('/portfolio', methods=['GET'])
@role_required('admin', 'treasurer', 'secretary')
def get_portfolio_view():
    p = get_portfolio()
    s = p['summary']

    active = []
    for a in p['active']:
        row = _serialize_loan(a)
        row['fee'] = str(a['fee'])
        row['repayments'] = [_repayment(r) for r in a['repayments']]
        active.append(row)

    q = p['queue']
    return jsonify({
        'summary': {
            'active_count': s['active_count'],
            'lent_out': str(s['lent_out']),
            'outstanding': str(s['outstanding']),
            'overdue_count': s['overdue_count'],
            'overdue_amount': str(s['overdue_amount']),
            'due_soon_count': s['due_soon_count'],
            'repaid_count': s['repaid_count'],
            'interest_collected': str(s['interest_collected']),
            'needs_action': s['needs_action'],
        },
        'aging': [{'key': b['key'], 'label': b['label'], 'count': b['count'],
                   'outstanding': str(b['outstanding'])} for b in p['aging']],
        'active': active,
        'queue': {
            'approval': [_serialize_loan(r) for r in q['approval']],
            'disbursement': [_serialize_loan(r) for r in q['disbursement']],
            'repayments': [{
                'id': r['id'],
                'loan_id': r['loan_id'],
                'name': r.get('full_name') or r['username'],
                'amount': str(r['amount']),
                'balance_after': _s(r.get('balance_after')),
                'submitted_at': _iso(r['submitted_at']),
            } for r in q['repayments']],
            'topups': [{
                'id': t['id'],
                'loan_id': t['loan_id'],
                'name': t.get('full_name') or t['username'],
                'amount': str(t['amount']),
                'fee': str(t['fee']),
                'requested_at': _iso(t['requested_at']),
            } for t in q['topups']],
        },
    }), 200


# Public: surcharge pool + fees collected, combined into one "group fund"
# figure per the plan (Section 6).
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


# A few numbers for the Home screen line and the nav badge. Members get
# their own loan; officers also get group-wide late / due-soon counts.
@loans_bp.route('/summary', methods=['GET'])
@token_required
def get_summary():
    u = request.user
    officer = bool(u.get('is_admin') or u.get('is_treasurer') or u.get('is_secretary'))
    sig = get_loan_signal(u.get('user_id'), officer=officer)

    mine = sig['mine']
    if mine:
        mine = {
            'status': mine['status'],
            'principal': str(mine['principal']),
            'attention': mine['attention'],
            'endorsement_count': mine.get('endorsement_count'),
            'outstanding': _s(mine.get('outstanding')),
            'awaiting_confirmation': _s(mine.get('awaiting_confirmation')),
            'due_at': _iso(mine.get('due_at')),
            'days_left': mine.get('days_left'),
            'days_overdue': mine.get('days_overdue'),
        }
    off = sig['officer']
    if off:
        off = {'overdue_count': off['overdue_count'], 'overdue_amount': str(off['overdue_amount']),
               'due_soon_count': off['due_soon_count']}
    return jsonify({'mine': mine, 'officer': off}), 200
