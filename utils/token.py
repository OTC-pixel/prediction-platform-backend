import jwt
import os
from flask import request, jsonify
from functools import wraps
import datetime

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. Set a long random value in .env -- "
        "never hardcode this."
    )

TOKEN_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "2"))


def generate_token(user_id, username, is_admin=False, is_treasurer=False):
    payload = {
        "user_id": user_id,
        "username": username,
        "is_admin": bool(is_admin),
        "is_treasurer": bool(is_treasurer),
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_TTL_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def _extract_token():
    auth_header = request.headers.get("Authorization", "")
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"error": "Token is missing"}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user = data
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Token has expired"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    """
    Usage: @role_required('admin')  or  @role_required('admin', 'treasurer')
    Requires a valid token AND at least one of the given roles (checked as
    is_<role> in the token payload). Admin is not implicitly included --
    list it explicitly if admins should also be allowed.
    """
    def wrapper(f):
        @wraps(f)
        @token_required
        def decorated(*args, **kwargs):
            user = getattr(request, "user", {}) or {}
            if not any(user.get(f"is_{r}") for r in roles):
                return jsonify({"error": "Forbidden: insufficient role"}), 403
            return f(*args, **kwargs)
        return decorated
    return wrapper


def is_admin():
    return getattr(request, "user", {}).get("is_admin", False)


def current_user_id():
    return getattr(request, "user", {}).get("user_id")
