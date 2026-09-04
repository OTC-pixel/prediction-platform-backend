from flask import Blueprint, request, jsonify, Response
from services.season_close import close_season, get_export, list_exports
from services.audit import get_audit_log
from utils.token import role_required

season_bp = Blueprint('season', __name__)


@season_bp.route('/close', methods=['POST'])
@role_required('admin')
def post_close():
    """Requires an explicit typed confirmation in the body -- not a fake
    client-side password (that was security theater sitting on top of
    already-real auth), just a deliberate speed-bump against a stray
    click on an irreversible action."""
    data = request.get_json(silent=True) or {}
    if data.get('confirm') != 'CLOSE SEASON':
        return jsonify({'message': 'Type CLOSE SEASON to confirm'}), 400

    ok, msg, export_id = close_season(request.user.get('user_id'))
    if ok:
        return jsonify({'message': 'Season closed', 'export_id': export_id}), 200
    return jsonify({'message': msg, 'export_id': export_id}), 500


@season_bp.route('/exports', methods=['GET'])
@role_required('admin', 'secretary')
def get_exports():
    rows = list_exports()
    return jsonify([
        {
            'id': r['id'],
            'created_at': r['created_at'].isoformat(),
            'created_by': r['created_by'],
        }
        for r in rows
    ]), 200


@season_bp.route('/exports/<int:export_id>/download', methods=['GET'])
@role_required('admin', 'secretary')
def download_export(export_id):
    row = get_export(export_id)
    if not row:
        return jsonify({'message': 'Export not found'}), 404
    filename = f"season-export-{row['created_at'].strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        row['csv_content'],
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@season_bp.route('/audit-log', methods=['GET'])
@role_required('admin', 'secretary')
def get_audit():
    rows = get_audit_log()
    return jsonify([
        {
            'id': r['id'],
            'actor_username': r['actor_username'],
            'action': r['action'],
            'target_type': r['target_type'],
            'target_id': r['target_id'],
            'metadata': r['metadata'],
            'created_at': r['created_at'].isoformat(),
        }
        for r in rows
    ]), 200
