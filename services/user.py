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
    finally:
        conn.close()


def verify_user(username, password):
    print(f"\n🔍 Verifying login for user: {username}")
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, password, is_approved, is_admin FROM users WHERE username = %s",
        (username,)
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        user_id = row['id']
        hashed_pw = row['password']
        is_approved = row['is_approved']
        is_admin = row['is_admin']

        print(f"👉 Found in DB: id={user_id}, is_approved={is_approved}, is_admin={is_admin}")
        print(f"🔑 Input password: {password}")
        print(f"🔒 Hashed in DB: {hashed_pw}")

        if check_password_hash(hashed_pw, password):
            print("✅ Password matches.")
            return {
                'id': user_id,
                'username': username,
                'is_approved': is_approved,
                'is_admin': is_admin
            }
        else:
            print("❌ Password does not match.")
    else:
        print("❌ Username not found in DB.")

    return None


def approve_user(username):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE users SET is_approved = 1 WHERE username = %s AND is_approved = 0",
        (username,)
    )
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0
