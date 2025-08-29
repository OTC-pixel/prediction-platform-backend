from werkzeug.security import generate_password_hash
from db import get_db  # uses psycopg2
from app import app    # import your Flask app

# Admin credentials
username = 'deogratious'
password = '11042004'
full_name = 'DEOGRATIOUS'
team = 'Liverpool'

hashed_pw = generate_password_hash(password)

with app.app_context():  # ✅ Enables Flask's g context
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Remove existing admin
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))

        # Insert new admin
        cursor.execute("""
            INSERT INTO users (username, password, full_name, team, is_approved, is_admin)
            VALUES (%s, %s, %s, %s, 1, 1)
        """, (username, hashed_pw, full_name, team))

        conn.commit()
        cursor.close()
        conn.close()

        print("✅ Admin user reset with username: admin and password: admin123")

    except Exception as e:
        print("❌ Failed to reset admin user:", e)
