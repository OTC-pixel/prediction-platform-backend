from flask import Blueprint, jsonify
from services.fixtures import get_current_matchday_fixtures
from flask_cors import cross_origin

fixtures_bp = Blueprint('fixtures', __name__)

@fixtures_bp.route('/current-matchday', methods=['GET'])
@cross_origin(origins=["http://localhost:3000"], supports_credentials=True)
def current_matchday():
    """
    Returns all fixtures for the current matchday.
    Includes proper CORS handling for credentials.
    """
    data = get_current_matchday_fixtures()

    if not data or not data.get("fixtures"):
        return jsonify({'message': 'No fixtures found for current matchday'}), 404

    return jsonify(data), 200
