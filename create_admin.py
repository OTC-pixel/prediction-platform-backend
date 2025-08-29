from werkzeug.security import generate_password_hash
from db import get_db
from app import app  # Import your Flask app instance

username = 'deogratious'
password = '11042004'
full_name = 'DEOGRATIOUS'
team = 'Liverpool'

hashed_pw = generate_password_hash(password)

with app.app_context():  # <-- THIS is the required fix
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Remove existing user with same username
        cursor.execute("DELETE FROM users WHERE username = %s", (username,))

        # Insert new admin user
        cursor.execute("""
            INSERT INTO users (username, password, full_name, team, is_approved, is_admin)
            VALUES (%s, %s, %s, %s, 1, 1)
        """, (username, hashed_pw, full_name, team))

        conn.commit()
        cursor.close()
        conn.close()

        print("✅ Admin user created with hashed password.")

    except Exception as e:
        print("❌ Failed to create admin:", e)
