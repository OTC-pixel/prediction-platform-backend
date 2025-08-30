from flask import Blueprint, request, jsonify
from services.user import create_user, verify_user
from utils.token import generate_token

auth_bp = Blueprint('auth', __name__)

# ✅ REGISTER - NO CORS, NO OPTIONS, JUST POST
@auth_bp.route("/register", methods=["POST"])
def register():
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

# ✅ LOGIN - NO CORS, NO OPTIONS, JUST POST
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    user = verify_user(username, password)

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