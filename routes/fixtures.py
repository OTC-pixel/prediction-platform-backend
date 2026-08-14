from flask import Blueprint, jsonify
from services.fixtures import get_current_matchday_fixtures
from flask_cors import cross_origin

fixtures_bp = Blueprint('fixtures', __name__)

@fixtures_bp.route('/current-matchday', methods=['GET'])
@cross_origin(origins=[
    "https://predict-eplt6.netlify.app",  # Your Netlify frontend
    "http://localhost:3000",              # Local development
    "http://localhost:3001",              # Alternative local port
    "http://127.0.0.1:3000",              # Local IP
    "http://127.0.0.1:3001"               # Alternative local IP
], supports_credentials=True)
def current_matchday():
    """
    Returns all fixtures for the current matchday.
    Includes proper CORS handling for credentials.
    """
    try:
        data = get_current_matchday_fixtures()

        if not data or not data.get("fixtures"):
            return jsonify({'message': 'No fixtures found for current matchday'}), 404

        return jsonify(data), 200
        
    except Exception as e:
        # Log the error for debugging
        print(f"Error in current_matchday: {str(e)}")
        return jsonify({'error': 'Internal server error', 'message': str(e)}), 500
