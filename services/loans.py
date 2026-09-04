r"""
Loans (rebuild plan, Section 6).

Lifecycle: pending -> endorsed -> approved -> disbursed -> repaid
                 \-> rejected (Treasurer can reject from pending/endorsed/approved)

"One active loan at a time" means: no loan in ('pending', 'endorsed',
'approved', 'disbursed') for that user. A rejected or fully repaid loan
doesn't block a new request.

Interest is captured at DISBURSEMENT, not at request time -- the plan
says the rate the Treasurer has configured applies "at disbursement", so
a rate change after a request but before disbursement uses the new rate.
`loans.interest_rate` is NULL until then.

Outstanding balance is always derived: principal + interest - confirmed
repayments, never stored. A loan closes (status -> 'repaid') the moment
a confirmation brings that number to <= 0.

Interest-collected aggregate (for the public group-fund figure): counted
only for fully repaid loans (principal*rate collected in full once
closed), not prorated across partial repayments on still-open loans.
Simpler and unambiguous; documented here since it's a real simplification
of "interest collected so far" rather than the only possible definition.
"""
from datetime import datetime, timezone
from db import get_db
from services.savings import get_user_savings_balance

REQUIRED_ENDORSEMENTS = 4
ACTIVE_STATUSES = ('pending', 'endorsed', 'approved', 'disbursed')


def _now():
    return datetime.now(timezone.utc)


def _get_active_loan_config(cur):
    cur.execute(
        "SELECT * FROM loan_config WHERE active = TRUE ORDER BY id DESC LIMIT 1"
    )
    return cur.fetchone()


# ---------- Config ----------

def set_loan_config(interest_rate, set_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE loan_config SET active = FALSE WHERE active = TRUE")
        cur.execute(
            """
            INSERT INTO loan_config (interest_rate, set_by, active)
            VALUES (%s, %s, TRUE)
            RETURNING *
            """,
            (interest_rate, set_by_user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row


def get_active_loan_config():
    conn = get_db()
    with conn.cursor() as cur:
        return _get_active_loan_config(cur)


# ---------- Request ----------

def request_loan(user_id, principal):
    if principal is None or float(principal) <= 0:
        return False, "Invalid amount", None

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM loans WHERE user_id = %s AND status = ANY(%s)",
            (user_id, list(ACTIVE_STATUSES)),
        )
        if cur.fetchone():
            return False, "You already have an active loan", None

        savings_balance = get_user_savings_balance(user_id)
        cap = float(savings_balance) * 0.75
        if float(principal) > cap:
            return False, f"Requested amount exceeds the 75% cap ({cap:.2f} based on your savings balance)", None

        cur.execute(
            """
            INSERT INTO loans (user_id, principal, status)
            VALUES (%s, %s, 'pending')
            RETURNING *
            """,
            (user_id, principal),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


# ---------- Endorsement ----------

def endorse_loan(loan_id, endorser_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "pending":
            return False, "This loan is no longer open for endorsement"
        if loan["user_id"] == endorser_user_id:
            return False, "You can't endorse your own loan request"

        cur.execute(
            "SELECT 1 FROM loan_endorsements WHERE loan_id = %s AND endorser_user_id = %s",
            (loan_id, endorser_user_id),
        )
        if cur.fetchone():
            return False, "You've already endorsed this loan"

        cur.execute(
            "INSERT INTO loan_endorsements (loan_id, endorser_user_id) VALUES (%s, %s)",
            (loan_id, endorser_user_id),
        )

        cur.execute(
            "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
            (loan_id,),
        )
        count = cur.fetchone()["count"]

        if count >= REQUIRED_ENDORSEMENTS:
            cur.execute(
                "UPDATE loans SET status = 'endorsed' WHERE id = %s", (loan_id,)
            )

        conn.commit()
        return True, None


def get_loans_pending_endorsement(viewer_user_id):
    """Loans still in 'pending' -- visible to all members since peer
    endorsement requires visibility. Includes whether the viewer has
    already endorsed, and excludes the viewer's own request."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.user_id, u.username, l.principal, l.requested_at
            FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'pending' AND l.user_id != %s
            ORDER BY l.requested_at ASC
            """,
            (viewer_user_id,),
        )
        loans = cur.fetchall()
        result = []
        for loan in loans:
            cur.execute(
                "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
                (loan["id"],),
            )
            count = cur.fetchone()["count"]
            cur.execute(
                "SELECT 1 FROM loan_endorsements WHERE loan_id = %s AND endorser_user_id = %s",
                (loan["id"], viewer_user_id),
            )
            already_endorsed = cur.fetchone() is not None
            result.append({
                **loan,
                "endorsement_count": count,
                "endorsements_needed": REQUIRED_ENDORSEMENTS,
                "already_endorsed": already_endorsed,
            })
        return result


# ---------- Approval / disbursement / rejection ----------

def get_loans_pending_approval():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'endorsed'
            ORDER BY l.requested_at ASC
            """
        )
        return cur.fetchall()


def get_loans_pending_disbursement():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username FROM loans l
            JOIN users u ON u.id = l.user_id
            WHERE l.status = 'approved'
            ORDER BY l.approved_at ASC
            """
        )
        return cur.fetchall()


def approve_loan(loan_id, approved_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "endorsed":
            return False, "Loan must reach the endorsement threshold before it can be approved"
        cur.execute(
            "UPDATE loans SET status = 'approved', approved_by = %s, approved_at = NOW() WHERE id = %s",
            (approved_by_user_id, loan_id),
        )
        conn.commit()
        return True, None


def reject_loan(loan_id, rejected_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] not in ("pending", "endorsed", "approved"):
            return False, f"Loan is already {loan['status']}"
        cur.execute(
            "UPDATE loans SET status = 'rejected', rejected_by = %s, rejected_at = NOW() WHERE id = %s",
            (rejected_by_user_id, loan_id),
        )
        conn.commit()
        return True, None


def disburse_loan(loan_id, disbursed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loans WHERE id = %s", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found"
        if loan["status"] != "approved":
            return False, "Loan must be approved before it can be disbursed"

        config = _get_active_loan_config(cur)
        if config is None:
            return False, "No interest rate has been configured"

        cur.execute(
            """
            UPDATE loans
            SET status = 'disbursed', interest_rate = %s, disbursed_by = %s, disbursed_at = NOW()
            WHERE id = %s
            """,
            (config["interest_rate"], disbursed_by_user_id, loan_id),
        )
        conn.commit()
        return True, None


# ---------- Repayments ----------

def _total_owed(loan):
    interest = (loan["principal"] * loan["interest_rate"] / 100) if loan["interest_rate"] else 0
    return loan["principal"] + interest


def _confirmed_repaid(cur, loan_id):
    cur.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM loan_repayments WHERE loan_id = %s AND status = 'confirmed'",
        (loan_id,),
    )
    return cur.fetchone()["total"]


def submit_repayment(loan_id, user_id, amount, idempotency_key):
    if amount is None or float(amount) <= 0:
        return False, "Invalid amount", None

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM loan_repayments WHERE idempotency_key = %s", (idempotency_key,)
        )
        existing = cur.fetchone()
        if existing:
            return True, "Already submitted", existing

        cur.execute("SELECT * FROM loans WHERE id = %s", (loan_id,))
        loan = cur.fetchone()
        if not loan:
            return False, "Loan not found", None
        if loan["user_id"] != user_id:
            return False, "This isn't your loan", None
        if loan["status"] != "disbursed":
            return False, "This loan isn't open for repayment", None

        cur.execute(
            """
            INSERT INTO loan_repayments (loan_id, amount, idempotency_key)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (loan_id, amount, idempotency_key),
        )
        row = cur.fetchone()
        conn.commit()
        return True, None, row


def confirm_repayment(repayment_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM loan_repayments WHERE id = %s", (repayment_id,))
        repayment = cur.fetchone()
        if not repayment:
            return False, "Repayment not found"
        if repayment["status"] != "pending":
            return False, f"Repayment is already {repayment['status']}"

        cur.execute(
            """
            UPDATE loan_repayments
            SET status = 'confirmed', confirmed_by = %s, confirmed_at = NOW()
            WHERE id = %s
            """,
            (confirmed_by_user_id, repayment_id),
        )

        cur.execute("SELECT * FROM loans WHERE id = %s", (repayment["loan_id"],))
        loan = cur.fetchone()
        total_owed = _total_owed(loan)
        repaid = _confirmed_repaid(cur, loan["id"])
        if repaid >= total_owed:
            cur.execute(
                "UPDATE loans SET status = 'repaid', closed_at = NOW() WHERE id = %s",
                (loan["id"],),
            )

        conn.commit()
        return True, None


def reject_repayment(repayment_id, confirmed_by_user_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM loan_repayments WHERE id = %s", (repayment_id,))
        repayment = cur.fetchone()
        if not repayment:
            return False, "Repayment not found"
        if repayment["status"] != "pending":
            return False, f"Repayment is already {repayment['status']}"
        cur.execute(
            """
            UPDATE loan_repayments
            SET status = 'rejected', confirmed_by = %s, confirmed_at = NOW()
            WHERE id = %s
            """,
            (confirmed_by_user_id, repayment_id),
        )
        conn.commit()
        return True, None


def get_pending_repayments():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.*, l.user_id, u.username FROM loan_repayments r
            JOIN loans l ON l.id = r.loan_id
            JOIN users u ON u.id = l.user_id
            WHERE r.status = 'pending'
            ORDER BY r.submitted_at ASC
            """
        )
        return cur.fetchall()


# ---------- Privacy-scoped reads ----------

def get_user_loans(user_id):
    """A member's own loans, with derived outstanding balance and
    repayment history -- private to the owner (and the Treasurer, via a
    separate call)."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM loans WHERE user_id = %s ORDER BY requested_at DESC",
            (user_id,),
        )
        loans = cur.fetchall()
        result = []
        for loan in loans:
            cur.execute(
                "SELECT * FROM loan_repayments WHERE loan_id = %s ORDER BY submitted_at ASC",
                (loan["id"],),
            )
            repayments = cur.fetchall()
            total_owed = _total_owed(loan) if loan["status"] in ("disbursed", "repaid") else None
            repaid = _confirmed_repaid(cur, loan["id"])
            cur.execute(
                "SELECT COUNT(*) AS count FROM loan_endorsements WHERE loan_id = %s",
                (loan["id"],),
            )
            endorsement_count = cur.fetchone()["count"]
            result.append({
                **loan,
                "endorsement_count": endorsement_count,
                "total_owed": total_owed,
                "outstanding": (total_owed - repaid) if total_owed is not None else None,
                "repayments": repayments,
            })
        return result


def get_all_loans_for_treasurer():
    """Full visibility for the Treasurer only -- matches the plan's
    'visible only to them and the Treasurer' privacy rule."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.*, u.username FROM loans l
            JOIN users u ON u.id = l.user_id
            ORDER BY l.requested_at DESC
            """
        )
        loans = cur.fetchall()
        result = []
        for loan in loans:
            repaid = _confirmed_repaid(cur, loan["id"])
            total_owed = _total_owed(loan) if loan["status"] in ("disbursed", "repaid") else None
            result.append({
                **loan,
                "total_owed": total_owed,
                "outstanding": (total_owed - repaid) if total_owed is not None else None,
            })
        return result


def get_interest_collected():
    """Aggregate figure for the public group-fund view -- see module
    docstring for why this only counts fully repaid loans."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT principal, interest_rate FROM loans WHERE status = 'repaid'"
        )
        total = 0
        for row in cur.fetchall():
            if row["interest_rate"]:
                total += row["principal"] * row["interest_rate"] / 100
        return total
