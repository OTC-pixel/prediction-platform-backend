from flask import Blueprint, request, jsonify, make_response
from services.user import create_user, verify_user
from flask_cors import cross_origin
from utils.token import generate_token
import os

# Environment-aware CORS origin
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")

auth_bp = Blueprint('auth', __name__)

# ✅ REGISTER
@auth_bp.route("/register", methods=["POST", "OPTIONS"])
@cross_origin(origin=FRONTEND_ORIGIN, supports_credentials=True)
def register():
    if request.method == 'OPTIONS':
        # Handle CORS preflight
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", FRONTEND_ORIGIN)
        response.headers.add("Access-Control-Allow-Headers", "Content-Type")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    full_name = data.get('fullName')
    team = data.get('team')

    if not all([username, password, full_name, team]):
        return jsonify({'message': 'All fields are required'}), 400

    success = create_user(username, password, full_name, team)
    if success:
        return jsonify({'message': 'Registration successful. Awaiting admin approval.'}), 200
    else:
        return jsonify({'message': 'Username already exists.'}), 400

# ✅ LOGIN
@auth_bp.route('/login', methods=['POST', 'OPTIONS'])
@cross_origin(origin=FRONTEND_ORIGIN, supports_credentials=True)
def login():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", FRONTEND_ORIGIN)
        response.headers.add("Access-Control-Allow-Headers", "Content-Type")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    print(f"\n🔐 Login attempt: {username} / {password}")
    user = verify_user(username, password)

    print(f"✅ User verification result: {user}")

    if user:
        if user['is_approved']:
            token = generate_token(username, user.get('is_admin', False))
            return jsonify({
                "token": token,
                "user": {
                    "id": user['id'],
                    "username": username,
                    "role": "admin" if user.get('is_admin') else "user"
                }
            }), 200
        else:
            return jsonify({"message": "Account pending admin approval"}), 403
    else:
        return jsonify({'message': 'Invalid credentials'}), 401
