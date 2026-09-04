"""
One-time admin bootstrap.

Replaces the old create_admin.py / reset_admin.py, which had a real
username and password hardcoded directly in source (twice, in two
near-duplicate files). This script reads credentials from .env instead,
refuses to run if they're missing, and refuses to create a duplicate
admin if one already exists with that username -- it will update the
password of an existing user instead, so it's safe to re-run.

Usage:
    1. Fill in ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_FULL_NAME / ADMIN_TEAM in .env
    2. python seed_admin.py
    3. Blank those .env values out again once done.
"""
import os
import sys
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()


def main():
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    full_name = os.getenv("ADMIN_FULL_NAME", "").strip()
    team = os.getenv("ADMIN_TEAM", "").strip()

    missing = [name for name, val in [
        ("ADMIN_USERNAME", username), ("ADMIN_PASSWORD", password),
        ("ADMIN_FULL_NAME", full_name), ("ADMIN_TEAM", team),
    ] if not val]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}. Fill these in and re-run.")
        sys.exit(1)

    if len(password) < 8:
        print("ADMIN_PASSWORD should be at least 8 characters.")
        sys.exit(1)

    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), cursor_factory=RealDictCursor,
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    )
    hashed = generate_password_hash(password)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    "UPDATE users SET password = %s, is_approved = 1, is_admin = 1 WHERE username = %s",
                    (hashed, username),
                )
                print(f"Existing user '{username}' updated to admin with new password.")
            else:
                cur.execute(
                    """
                    INSERT INTO users (username, password, full_name, team, is_approved, is_admin)
                    VALUES (%s, %s, %s, %s, 1, 1)
                    """,
                    (username, hashed, full_name, team),
                )
                print(f"Admin user '{username}' created.")
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
