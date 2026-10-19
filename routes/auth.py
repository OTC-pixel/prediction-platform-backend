from flask import Blueprint, request, jsonify
from services.user import create_user, verify_user
from services.password_reset import request_password_reset, change_own_password
from utils.token import generate_token, token_required, current_user_id
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
    username = (data.get('username') or '').strip()
    password = data.get('password')

    user = verify_user(username, password)

    if user:
        if user['is_approved']:
            token = generate_token(
                user_id=user['id'],
                username=username,
                is_admin=user.get('is_admin', False),
                is_treasurer=user.get('is_treasurer', False),
                is_secretary=user.get('is_secretary', False),
            )
            return jsonify({
                "token": token,
                "user": {
                    "id": user['id'],
                    "username": username,
                    "role": "admin" if user.get('is_admin') else "user",
                    "is_treasurer": bool(user.get('is_treasurer')),
                    "is_secretary": bool(user.get('is_secretary')),
                    "must_change_password": bool(user.get('must_change_password')),
                }
            }), 200
        else:
            return jsonify({"message": "Account pending admin approval"}), 403
    else:
        return jsonify({'message': 'Invalid credentials'}), 401


# ✅ FORGOT PASSWORD (public) -- admin-mediated, no email involved.
# Creates a pending request an admin will see in User Management.
# Deliberately returns the same response whether or not the username
# exists, so this endpoint can't be used to enumerate valid usernames.
@auth_bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    if not username:
        return jsonify({'message': 'Username is required'}), 400

    try:
        request_password_reset(username)
    except Exception as e:
        print(f"forgot_password error: {e}")
        # Still return the generic success message -- don't leak
        # whether the failure means the username didn't exist or
        # something actually broke.

    return jsonify({'message': 'If the account exists, an admin has been notified.'}), 200


# ✅ CHANGE OWN PASSWORD -- used both for the forced change after an
# admin-generated one-time password, and for a routine voluntary change.
# Requires a valid token, i.e. the user must already be logged in
# (including via the one-time password) before calling this.
@auth_bp.route('/change-password', methods=['POST'])
@token_required
def change_password():
    data = request.get_json() or {}
    new_password = data.get('new_password')
    if not new_password or len(new_password) < 6:
        return jsonify({'message': 'Password must be at least 6 characters'}), 400

    success = change_own_password(current_user_id(), new_password)
    if success:
        return jsonify({'message': 'Password updated'}), 200
    return jsonify({'message': 'Failed to update password'}), 400
