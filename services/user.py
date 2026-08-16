from db import get_db
from werkzeug.security import generate_password_hash, check_password_hash


def create_user(username, password, full_name, team):
    conn = get_db()
    cursor = conn.cursor()
    try:
        hashed_pw = generate_password_hash(password)
        cursor.execute(
            """
            INSERT INTO users (username, password, full_name, team, is_approved)
            VALUES (%s, %s, %s, %s, 0)
            """,
            (username, hashed_pw, full_name, team)
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Error creating user: {e}")
        conn.rollback()
        return False


def verify_user(username, password):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, password, is_approved, is_admin, is_treasurer, is_secretary FROM users WHERE username = %s",
        (username,)
    )
    row = cursor.fetchone()

    if not row:
        return None

    if not check_password_hash(row['password'], password):
        return None

    return {
        'id': row['id'],
        'username': username,
        'is_approved': row['is_approved'],
        'is_admin': row['is_admin'],
        'is_treasurer': row.get('is_treasurer', False),
        'is_secretary': row.get('is_secretary', False),
    }
