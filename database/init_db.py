import psycopg2
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

def init_db():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    cursor = conn.cursor()

    # USERS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT,
            team TEXT,
            is_approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )
    ''')

    # FIXTURES TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fixtures (
            id SERIAL PRIMARY KEY,
            fixture_id INTEGER UNIQUE,
            matchday INTEGER NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_time TEXT NOT NULL,
            result TEXT DEFAULT NULL
        )
    ''')

    # PREDICTIONS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS predictions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            fixture_id INTEGER,
            predicted_result TEXT,
            points_awarded INTEGER DEFAULT 0,
            final_result TEXT DEFAULT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(fixture_id) REFERENCES fixtures(fixture_id)
        )
    ''')

    # MATCHDAY TRACKER TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matchday_tracker (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_matchday INTEGER DEFAULT 0,
            last_completed_matchday INTEGER DEFAULT 0,
            last_updated TEXT
        )
    ''')

    # Insert initial tracker row if not exists
    initial_time = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()
    cursor.execute('''
        INSERT INTO matchday_tracker (id, current_matchday, last_completed_matchday, last_updated)
        VALUES (1, 0, 0, %s)
        ON CONFLICT (id) DO NOTHING
    ''', (initial_time,))

    # RESULTS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS results (
            matchday INTEGER PRIMARY KEY,
            results_json TEXT NOT NULL,
            results_text TEXT DEFAULT NULL,
            updated_at TEXT NOT NULL
        )
    ''')

    # MATCHDAY RESULTS TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matchday_results (
            id SERIAL PRIMARY KEY,
            matchday INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER DEFAULT 0,
            UNIQUE(matchday, user_id)
        )
    ''')

    # LEADERBOARD TABLE
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leaderboard (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 0,
            current_matchday INTEGER DEFAULT 0,
            last_updated TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print("Database successfully created.")
