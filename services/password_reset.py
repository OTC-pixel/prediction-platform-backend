"""
Admin-mediated forgot-password flow.

No SMTP/email infra exists in this app, and the pattern for
account-related approvals here is already admin-mediated (pending-user
approval, treasurer payment confirmation) -- so a forgotten password
follows the same shape: user submits a request, an admin sees it in
User Management and generates a one-time password, relays it to the
user out-of-band (chat/in person), and the user is forced to set their
own password the moment they log in with it.
"""
import secrets
import string
from db import get_db
from werkzeug.security import generate_password_hash

# Characters chosen to avoid visual ambiguity (no 0/O, 1/l/I) since this
# gets read off a screen and retyped or relayed by voice/chat.
_TEMP_PW_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_temp_password(length=10):
    return ''.join(secrets.choice(_TEMP_PW_ALPHABET) for _ in range(length))


def request_password_reset(username):
    """
    Create a pending reset request for `username` if the account exists
    and doesn't already have one pending. Always safe to call for a
    username that doesn't exist -- it's a no-op, and the route returns
    the same response either way so this can't be used to enumerate
    valid usernames.
    """
    username = (username or '').strip()
    if not username:
        return

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if not row:
            return  # silently no-op; route response doesn't reveal this

        user_id = row['id']

        cur.execute(
            "SELECT id FROM password_reset_requests WHERE username = %s AND status = 'pending'",
            (username,)
        )
        if cur.fetchone():
            return  # already has one pending, don't create a duplicate

        cur.execute(
            "INSERT INTO password_reset_requests (user_id, username) VALUES (%s, %s)",
            (user_id, username)
        )
        conn.commit()


def get_pending_password_reset_requests():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.username, r.created_at, u.full_name, u.team
            FROM password_reset_requests r
            JOIN users u ON u.id = r.user_id
            WHERE r.status = 'pending'
            ORDER BY r.created_at ASC
            """
        )
        rows = cur.fetchall()
        return [{
            'id': r['id'],
            'username': r['username'],
            'fullName': r['full_name'],
            'team': r['team'],
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
        } for r in rows]


def admin_reset_password(username, actor_id):
    """
    Generates a fresh one-time password for `username`, sets it
    immediately (overwriting whatever they had), flags the account so
    the very next successful login must be followed by the user setting
    their own password, and resolves any pending reset request for this
    username. Returns the plaintext temp password -- this is the only
    place/time it exists in plaintext; only the admin who just called
    this sees it, to relay manually.
    """
    username = (username or '').strip()
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if not row:
            return None
        user_id = row['id']

        temp_password = _generate_temp_password()
        hashed = generate_password_hash(temp_password)

        cur.execute(
            "UPDATE users SET password = %s, must_change_password = 1 WHERE id = %s",
            (hashed, user_id)
        )
        cur.execute(
            """
            UPDATE password_reset_requests
            SET status = 'resolved', resolved_at = NOW(), resolved_by = %s
            WHERE username = %s AND status = 'pending'
            """,
            (actor_id, username)
        )
        conn.commit()
        return temp_password


def change_own_password(user_id, new_password):
    """
    User setting their own password -- either the forced change after an
    admin-generated one-time password, or a routine voluntary change.
    Clears must_change_password either way.
    """
    hashed = generate_password_hash(new_password)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password = %s, must_change_password = 0 WHERE id = %s",
            (hashed, user_id)
        )
        conn.commit()
        return cur.rowcount > 0
