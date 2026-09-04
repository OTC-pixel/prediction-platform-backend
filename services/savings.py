"""
Savings & surcharge ("Section 4" of the rebuild plan).

Design notes
------------
Weekly cycle: ISO weeks, Monday 00:00 -> next Monday 00:00. A transaction's
`week_start` is the Monday of the ISO week it was submitted in. No manual
week creation -- weeks just exist as date ranges.

Allocation on confirm (the "FIFO surcharge clearing" rule): surcharge is
now cleared AUTOMATICALLY, off the top of every confirmed transaction --
no exception, no approval, no treasurer choice involved. Oldest surcharge
first (FIFO); whatever's left after every outstanding surcharge is
cleared counts toward that week's minimum as normal.

This replaces an earlier "minimum-first by default, surcharge-first only
via an approved exception" design. That design only ever cleared a
surcharge when a member happened to send MORE than the weekly minimum in
one transaction -- in practice, members almost always send close to
exactly the minimum, so surplus rarely occurred and surcharges piled up
uncollected indefinitely. Automatic-first fixes that, at a real
trade-off worth stating plainly: if a member's transaction is only large
enough to cover that week's minimum, some of it now goes to surcharge
instead, which can leave that same week short and risk a NEW surcharge
at the next rollover -- a possible snowball for a member who only ever
sends the bare minimum. There's no floor/cap protecting against that
here; if that trade-off turns out to be a problem in practice, the fix
is a cap on how much of a single transaction surcharge-clearing can
claim before minimum-protection kicks back in.

Balances (savings balance, surcharge owed, surcharge cleared) are never
stored -- every read recomputes them from the transaction/clearance
history, per the plan's "derived, not stored" discipline.

Note on the `exception_requests` table: it was originally built to gate
a surcharge-priority override that required explicit Treasurer approval.
That override no longer exists as of the automatic-surcharge-clearing
change above -- nothing in this codebase writes to that table anymore.
The table itself is left in place (unused) rather than dropped, since
removing schema is more consequential than removing dead code paths;
`services/season_close.py` still clears it during a season wipe, which
is a harmless no-op against an always-empty table.
"""
from datetime import datetime, timezone, timedelta, date
from db import get_db


def _now():
    return datetime.now(timezone.utc)


def _week_start(d=None):
    """Monday of the ISO week containing d (defaults to today, UTC)."""
    d = d or _now().date()
    return d - timedelta(days=d.weekday())


def _get_active_savings_config(cur):
    cur.execute(
        "SELECT * FROM savings_config WHERE active = TRUE ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


# ---------- Config ----------

def set_savings_config(weekly_minimum, surcharge_amount, set_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE savings_config SET active = FALSE WHERE active = TRUE")
        cur.execute(
            """
            INSERT INTO savings_config (weekly_minimum, surcharge_amount, set_by, active)
            VALUES (%s, %s, %s, TRUE)
            RETURNING *
            """,
            (weekly_minimum, surcharge_amount, set_by_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def get_active_savings_config():
    conn = get_db()
    with conn.cursor() as cur:
        return _get_active_savings_config(cur)


# ---------- Submitting & confirming transactions ----------

def submit_transaction(user_id, amount, idempotency_key):
    if amount is None or float(amount) <= 0:
        return False, "Invalid amount", None

    conn = get_db()
    with conn.cursor() as cur:
        # True idempotency: if this key was already used, return the
        # existing record instead of erroring -- a double-tap resubmit is
        # a no-op, not a duplicate transaction.
        cur.execute(
            "SELECT * FROM savings_transactions WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        existing = cur.fetchone()
        if existing:
            return True, "Already submitted", existing

        cur.execute(
            """
            INSERT INTO savings_transactions (user_id, amount, week_start, idempotency_key)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, amount, _week_start(), idempotency_key),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


def _confirmed_week_total(cur, user_id, week_start):
    cur.execute(
        """
        SELECT COALESCE(SUM(allocated_savings), 0) AS total
        FROM savings_transactions
        WHERE user_id = %s AND week_start = %s AND status = 'confirmed'
        """,
        (user_id, week_start),
    )
    return cur.fetchone()["total"]


def _outstanding_surcharges(cur, user_id):
    """FIFO list of (surcharge_row, remaining_owed) oldest first."""
    cur.execute(
        "SELECT * FROM surcharge_ledger WHERE user_id = %s ORDER BY week_start ASC",
        (user_id,),
    )
    surcharges = cur.fetchall()
    result = []
    for s in surcharges:
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) AS cleared FROM surcharge_clearances WHERE surcharge_id = %s",
            (s["id"],),
        )
        cleared = cur.fetchone()["cleared"]
        remaining = s["amount"] - cleared
        if remaining > 0:
            result.append((s, remaining))
    return result


def confirm_transaction(transaction_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM savings_transactions WHERE id = %s", (transaction_id,))
        txn = cur.fetchone()
        if not txn:
            return False, "Transaction not found"
        if txn["status"] != "pending":
            return False, f"Transaction is already {txn['status']}"

        amount = txn["amount"]
        user_id = txn["user_id"]

        remaining = amount
        surcharge_allocated = 0

        # Surcharge first, automatically, oldest owed first -- see the
        # module docstring for why this changed from the earlier
        # minimum-first default.
        for surcharge_row, owed in _outstanding_surcharges(cur, user_id):
            if remaining <= 0:
                break
            clear_amount = min(remaining, owed)
            cur.execute(
                """
                INSERT INTO surcharge_clearances (surcharge_id, savings_transaction_id, amount)
                VALUES (%s, %s, %s)
                """,
                (surcharge_row["id"], transaction_id, clear_amount),
            )
            remaining -= clear_amount
            surcharge_allocated += clear_amount

        to_savings = remaining

        cur.execute(
            """
            UPDATE savings_transactions
            SET status = 'confirmed', confirmed_by = %s, confirmed_at = NOW(),
                allocated_savings = %s, allocated_surcharge = %s
            WHERE id = %s
            """,
            (confirmed_by_user_id, to_savings, surcharge_allocated, transaction_id),
        )
        conn.commit()
        return True, None


def reject_transaction(transaction_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM savings_transactions WHERE id = %s", (transaction_id,)
        )
        row = cur.fetchone()
        if not row:
            return False, "Transaction not found"
        if row["status"] != "pending":
            return False, f"Transaction is already {row['status']}"
        cur.execute(
            """
            UPDATE savings_transactions
            SET status = 'rejected', confirmed_by = %s, confirmed_at = NOW()
            WHERE id = %s
            """,
            (confirmed_by_user_id, transaction_id),
        )
        conn.commit()
        return True, None


def get_pending_transactions():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.*, u.username
            FROM savings_transactions t
            JOIN users u ON u.id = t.user_id
            WHERE t.status = 'pending'
            ORDER BY t.submitted_at ASC
            """
        )
        return cur.fetchall()


# ---------- Weekly rollover (surcharge accrual) ----------

def process_week_rollover():
    """
    For every fully-closed ISO week since the last processed one, charge a
    surcharge to any approved user whose confirmed savings total for that
    week fell below the weekly minimum. Idempotent: re-running is always
    safe -- surcharge_ledger has a UNIQUE(user_id, week_start) constraint
    and the tracker only ever advances.
    """
    conn = get_db()
    with conn.cursor() as cur:
        config = _get_active_savings_config(cur)
        if config is None:
            return  # nothing to enforce yet

        cur.execute("SELECT last_processed_week FROM savings_tracker WHERE id = 1")
        row = cur.fetchone()
        last_processed = row["last_processed_week"] if row else None

        current_week = _week_start()
        # Start the day after the last processed week's Monday+7, or the
        # week this config was first created if never processed.
        next_week = (
            last_processed + timedelta(days=7)
            if last_processed
            else _week_start(config["created_at"].date())
        )

        weeks_processed = 0
        while next_week < current_week and weeks_processed < 52:
            cur.execute(
                "SELECT id FROM users WHERE is_approved = 1"
            )
            user_ids = [r["id"] for r in cur.fetchall()]

            for user_id in user_ids:
                total = _confirmed_week_total(cur, user_id, next_week)
                if total < config["weekly_minimum"]:
                    cur.execute(
                        """
                        INSERT INTO surcharge_ledger (user_id, week_start, amount)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id, week_start) DO NOTHING
                        """,
                        (user_id, next_week, config["surcharge_amount"]),
                    )

            cur.execute(
                "UPDATE savings_tracker SET last_processed_week = %s WHERE id = 1",
                (next_week,),
            )
            conn.commit()
            next_week += timedelta(days=7)
            weeks_processed += 1


# ---------- Config history & audit ----------

def get_savings_config_history():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.weekly_minimum, c.surcharge_amount, c.created_at, u.username AS set_by
            FROM savings_config c
            LEFT JOIN users u ON u.id = c.set_by
            ORDER BY c.created_at DESC
            """
        )
        return cur.fetchall()


# ---------- Derived balances & views ----------

def get_user_savings_balance(user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(allocated_savings), 0) AS balance
            FROM savings_transactions
            WHERE user_id = %s AND status = 'confirmed'
            """,
            (user_id,),
        )
        return cur.fetchone()["balance"]


def get_user_ledger(user_id):
    """Personal ledger: every transaction the user submitted, plus a
    running savings balance after each confirmed one, plus their
    surcharge weeks."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM savings_transactions
            WHERE user_id = %s
            ORDER BY submitted_at ASC
            """,
            (user_id,),
        )
        txns = cur.fetchall()

        running_balance = 0
        ledger = []
        for t in txns:
            if t["status"] == "confirmed":
                running_balance += t["allocated_savings"]
            ledger.append({
                "id": t["id"],
                "amount": t["amount"],
                "week_start": t["week_start"].isoformat(),
                "submitted_at": t["submitted_at"].isoformat(),
                "status": t["status"],
                "confirmed_at": t["confirmed_at"].isoformat() if t["confirmed_at"] else None,
                "allocated_savings": t["allocated_savings"],
                "allocated_surcharge": t["allocated_surcharge"],
                "running_balance": running_balance,
            })

        cur.execute(
            "SELECT * FROM surcharge_ledger WHERE user_id = %s ORDER BY week_start ASC",
            (user_id,),
        )
        surcharges = []
        for s in cur.fetchall():
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) AS cleared FROM surcharge_clearances WHERE surcharge_id = %s",
                (s["id"],),
            )
            cleared = cur.fetchone()["cleared"]
            surcharges.append({
                "week_start": s["week_start"].isoformat(),
                "amount": s["amount"],
                "cleared": cleared,
                "owed": s["amount"] - cleared,
            })

        return {
            "transactions": ledger,
            "savings_balance": running_balance,
            "surcharges": surcharges,
        }


def get_total_savings_balance():
    """Grand total across every member's confirmed savings -- the figure
    that should tally against physical/mobile cash on hand, growing over
    time as more gets confirmed. Never stored, always derived."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(allocated_savings), 0) AS total FROM savings_transactions WHERE status = 'confirmed'"
        )
        return cur.fetchone()["total"]


def get_members_savings_overview():
    """One row per approved member: current savings balance and total
    surcharge still owed -- the collapsed-row view for the Treasurer/
    Secretary cash-reconciliation roster, before drilling into any one
    member's full history."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, full_name FROM users WHERE is_approved = 1 ORDER BY username"
        )
        users = cur.fetchall()
        result = []
        for u in users:
            cur.execute(
                "SELECT COALESCE(SUM(allocated_savings), 0) AS balance FROM savings_transactions WHERE user_id = %s AND status = 'confirmed'",
                (u["id"],),
            )
            balance = cur.fetchone()["balance"]

            cur.execute("SELECT id, amount FROM surcharge_ledger WHERE user_id = %s", (u["id"],))
            owed = 0
            for s in cur.fetchall():
                cur.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS cleared FROM surcharge_clearances WHERE surcharge_id = %s",
                    (s["id"],),
                )
                owed += s["amount"] - cur.fetchone()["cleared"]

            result.append({
                "user_id": u["id"],
                "username": u["username"],
                "full_name": u["full_name"],
                "savings_balance": balance,
                "surcharge_owed": owed,
            })
        return result


def get_surcharge_pool():
    """Public view: who owes what (and since when), plus group totals.
    Deliberately public per the plan -- shared-fund transparency."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.user_id, u.username, s.week_start, s.amount
            FROM surcharge_ledger s
            JOIN users u ON u.id = s.user_id
            ORDER BY s.week_start ASC
            """
        )
        rows = cur.fetchall()

        breakdown = []
        total_owed = 0
        total_charged = 0
        for r in rows:
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) AS cleared FROM surcharge_clearances WHERE surcharge_id = %s",
                (r["id"],),
            )
            cleared = cur.fetchone()["cleared"]
            owed = r["amount"] - cleared
            total_charged += r["amount"]
            total_owed += owed
            if owed > 0:
                breakdown.append({
                    "username": r["username"],
                    "week_start": r["week_start"].isoformat(),
                    "owed": owed,
                })

        return {
            "total_charged": total_charged,
            "total_collected": total_charged - total_owed,
            "total_owed": total_owed,
            "owing": breakdown,
        }
