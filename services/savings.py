"""
Savings & surcharge ("Section 4" of the rebuild plan).

Design notes
------------
Weekly cycle: ISO weeks, Monday 00:00 -> next Monday 00:00. A transaction's
`week_start` is the Monday of the ISO week it was submitted in. No manual
week creation -- weeks just exist as date ranges.

Allocation on confirm (the "FIFO surcharge clearing" rule): two modes,
selected by the Treasurer per-confirmation via `prioritize_surcharge`:

  - Default (prioritize_surcharge=False): minimum-first. The amount tops
    up this week's confirmed total toward the weekly minimum first; only
    the true surplus beyond the minimum clears outstanding surcharges,
    oldest first (FIFO). This path never needs an exception, because it
    never takes money away from the current week to pay off a debt -- it
    can only help.
  - Override (prioritize_surcharge=True): surcharge-first. The whole
    amount goes toward clearing the oldest outstanding surcharge(s)
    before anything counts toward this week's minimum -- which can leave
    the user still short for the week. Per the plan, this deliberately
    weaker-for-the-week path requires an approved, not-yet-used
    `exception_requests` row of type 'surcharge_priority' for this user;
    confirming with this flag consumes that approval.

This is the concrete reading of "the treasurer cannot confirm it as fully
clearing... it either applies the whole surplus toward the surcharge and
leaves the user still short for the week, or the exception mechanism is
used" -- those are exactly the two modes above.

Balances (savings balance, surcharge owed, surcharge cleared) are never
stored -- every read recomputes them from the transaction/clearance
history, per the plan's "derived, not stored" discipline.

Note on exception_requests scope: the plan's sketch (Section 9) lists this
table with `type[commitment_fee/surcharge]` as if one workflow covered
both. Phase 2 was already built and shipped with the Treasurer granting
commitment-fee exceptions directly (no user request step) before this
table existed. Rather than rebuild shipped Phase 2 behavior, this table
is scoped to `type = 'surcharge_priority'` only for now -- a real
inconsistency with "one exception-approval pattern reused everywhere",
worth reconciling in a later pass, not silently glossed over.
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


def _consume_surcharge_priority_exception(cur, user_id):
    cur.execute(
        """
        SELECT id FROM exception_requests
        WHERE user_id = %s AND type = 'surcharge_priority' AND status = 'approved'
        ORDER BY decided_at ASC LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    cur.execute(
        "UPDATE exception_requests SET status = 'used' WHERE id = %s", (row["id"],)
    )
    return True


def confirm_transaction(transaction_id, confirmed_by_user_id, prioritize_surcharge=False):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM savings_transactions WHERE id = %s", (transaction_id,))
        txn = cur.fetchone()
        if not txn:
            return False, "Transaction not found"
        if txn["status"] != "pending":
            return False, f"Transaction is already {txn['status']}"

        config = _get_active_savings_config(cur)
        weekly_minimum = config["weekly_minimum"] if config else 0
        amount = txn["amount"]
        user_id = txn["user_id"]

        if prioritize_surcharge:
            if not _consume_surcharge_priority_exception(cur, user_id):
                conn.commit()
                return False, "No approved surcharge-priority exception for this user"
            to_savings = 0
            surplus = amount
        else:
            week_total_before = _confirmed_week_total(cur, user_id, txn["week_start"])
            remaining_to_minimum = max(0, weekly_minimum - week_total_before)
            to_savings = min(amount, remaining_to_minimum)
            surplus = amount - to_savings

        surcharge_allocated = 0
        if surplus > 0:
            for surcharge_row, remaining in _outstanding_surcharges(cur, user_id):
                if surplus <= 0:
                    break
                clear_amount = min(surplus, remaining)
                cur.execute(
                    """
                    INSERT INTO surcharge_clearances (surcharge_id, savings_transaction_id, amount)
                    VALUES (%s, %s, %s)
                    """,
                    (surcharge_row["id"], transaction_id, clear_amount),
                )
                surplus -= clear_amount
                surcharge_allocated += clear_amount
            # Anything left after every surcharge is cleared is bonus
            # savings principal beyond the minimum.
            if surplus > 0:
                to_savings += surplus
                surplus = 0

        cur.execute(
            """
            UPDATE savings_transactions
            SET status = 'confirmed', confirmed_by = %s, confirmed_at = NOW(),
                allocated_savings = %s, allocated_surcharge = %s, prioritize_surcharge = %s
            WHERE id = %s
            """,
            (confirmed_by_user_id, to_savings, surcharge_allocated, prioritize_surcharge, transaction_id),
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


# ---------- Exception requests ----------

def request_exception(user_id, exception_type, context):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO exception_requests (user_id, type, context, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING *
            """,
            (user_id, exception_type, context),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def decide_exception_request(request_id, approve, decided_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM exception_requests WHERE id = %s", (request_id,)
        )
        row = cur.fetchone()
        if not row:
            return False, "Request not found"
        if row["status"] != "pending":
            return False, f"Request is already {row['status']}"
        cur.execute(
            """
            UPDATE exception_requests
            SET status = %s, decided_by = %s, decided_at = NOW()
            WHERE id = %s
            """,
            ("approved" if approve else "rejected", decided_by_user_id, request_id),
        )
        conn.commit()
        return True, None


def get_exception_requests(status=None):
    conn = get_db()
    with conn.cursor() as cur:
        if status:
            cur.execute(
                """
                SELECT e.*, u.username FROM exception_requests e
                JOIN users u ON u.id = e.user_id
                WHERE e.status = %s ORDER BY e.created_at DESC
                """,
                (status,),
            )
        else:
            cur.execute(
                """
                SELECT e.*, u.username FROM exception_requests e
                JOIN users u ON u.id = e.user_id
                ORDER BY e.created_at DESC
                """
            )
        return cur.fetchall()


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


def get_decided_surcharge_exceptions():
    """Approved/rejected/used surcharge-priority requests -- the decision
    is the auditable event, not the raw request."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, u.username, e.status, e.decided_at, d.username AS decided_by
            FROM exception_requests e
            JOIN users u ON u.id = e.user_id
            LEFT JOIN users d ON d.id = e.decided_by
            WHERE e.type = 'surcharge_priority' AND e.decided_at IS NOT NULL
            ORDER BY e.decided_at DESC
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
