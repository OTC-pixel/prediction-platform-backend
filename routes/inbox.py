from flask import Blueprint, request, jsonify
from services.inbox import get_inbox
from utils.token import token_required, current_roles, current_user_id

inbox_bp = Blueprint('inbox', __name__)


@inbox_bp.route('/summary', methods=['GET'])
@token_required
def summary():
    """Everything waiting on the caller, for the home card, nav badge and
    tab title. Members with no admin/treasurer/secretary role simply get
    an empty inbox."""
    roles = current_roles()
    if not roles:
        return jsonify({'total': 0, 'counts': {}, 'sections': {}, 'degraded': [], 'items': []}), 200

    try:
        limit = max(1, min(int(request.args.get('limit', 5)), 20))
    except ValueError:
        limit = 5

    return jsonify(get_inbox(current_user_id(), roles, limit)), 200
