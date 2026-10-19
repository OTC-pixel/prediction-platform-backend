"""
Generic approval routes -- thin wrapper around services/approvals.py.
Works for ANY registered workflow type (see WORKFLOWS there); this
file has no commitment-fee-specific logic in it at all.
"""
from flask import Blueprint, request, jsonify
from services.approvals import get_pending_for_roles, decide
from utils.token import token_required, current_roles

approvals_bp = Blueprint('approvals', __name__)


@approvals_bp.route('/pending', methods=['GET'])
@token_required
def pending():
    roles = current_roles()
    if not roles:
        return jsonify([]), 200
    items = get_pending_for_roles(roles, request.user.get('user_id'))
    return jsonify(items), 200


@approvals_bp.route('/<int:request_id>/decide', methods=['POST'])
@token_required
def post_decide(request_id):
    data = request.get_json(silent=True) or {}
    decision = data.get('decision')
    role = data.get('role')  # which of the caller's own roles they're acting as

    roles = current_roles()
    if role not in roles:
        return jsonify({'message': "You don't hold that role"}), 403

    ok, message = decide(request_id, role, request.user.get('user_id'), decision)
    if ok:
        return jsonify({'message': message}), 200
    return jsonify({'message': message}), 400
