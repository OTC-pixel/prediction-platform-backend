from flask import Blueprint, request, jsonify
from services.user import create_user, verify_user
from utils.token import generate_token
import re

auth_bp = Blueprint('auth', __name__)

# ✅ REGISTER
@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    full_name = data.get('fullName')
    team = data.get('team')

    if not all([username, password, full_name, team]):
        return jsonify({'message': 'All fields are required'}), 400

    # ✅ Step 1: Trim whitespace
    cleaned_username = username.strip()

    # 1️⃣ Leading/trailing spaces
    if cleaned_username != username:
        return jsonify({'message': 'Username cannot start or end with spaces'}), 400

    # 2️⃣ Reject emails
    email_regex = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    if re.match(email_regex, cleaned_username):
        return jsonify({'message': 'Username cannot be an email address'}), 400

    # 3️⃣ Only letters, numbers, spaces
    if not re.match(r'^[a-zA-Z0-9 ]+$', cleaned_username):
        return jsonify({'message': 'Username can only contain letters, numbers, and spaces'}), 400

    # ✅ Step 2: Create user
    success = create_user(cleaned_username, password, full_name, team)
    if success:
        return jsonify({'message': 'Registration successful. Awaiting admin approval.'}), 200
    else:
        return jsonify({'message': 'Username already exists.'}), 400


# ✅ LOGIN
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
