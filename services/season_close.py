"""
Season close (rebuild plan, Section 7 / Phase 5).

Order matters here and is enforced, not just documented: the export is
generated and durably stored FIRST, and the wipe only proceeds if that
succeeds. If the export fails, nothing is deleted -- a failed export
should never be able to silently destroy the season's history.

The export is stored as a row in the DB (season_exports.csv_content),
not written to disk -- Render's filesystem is ephemeral and doesn't
survive a redeploy or restart, so a file on disk would not actually be
durable.

Scope of the wipe: everything except the users table itself. Competition
data (predictions, fixtures, results, leaderboard), financial HISTORY
(savings transactions, surcharge ledger, loans and their endorsements/
repayments, commitment-fee status and exceptions), rule/config tables
(commitment_fee_config, savings_config, loan_config), and the admin
audit log are all cleared. The one deliberate exception: season_exports
(the CSV backups this same close generates) is never touched -- wiping
it would destroy the archival record in the same action that just
created it, defeating the entire point of exporting before resetting.
Users are always preserved; only the treasurer/secretary flags are
stripped (admin is untouched), matching Phase 0's existing behavior.
"""
from datetime import datetime, timezone
import csv
import io
from db import get_db
from services.audit import log_action


def _generate_export_csv(cur):
    """One row per approved user: final savings balance, surcharge
    paid/owed, loan status -- plus final leaderboard standing, since
    that's about to be wiped too and is exactly the kind of thing a
    group wants a permanent record of."""
    cur.execute(
        "SELECT id, username, full_name FROM users WHERE is_approved = 1 ORDER BY username"
    )
    users = cur.fetchall()

    # There is no stored "rank" column -- services/leaderboard.py computes
    # rank purely as array position after this exact ordering (points
    # DESC, username ASC as the tie-break). Reproduced here so the
    # exported rank always matches what the live leaderboard showed.
    cur.execute(
        """
        SELECT l.user_id, l.points, u.username
        FROM leaderboard l
        JOIN users u ON u.id = l.user_id
        WHERE u.is_approved = 1
        ORDER BY l.points DESC, u.username ASC
        """
    )
    ranked = cur.fetchall()
    leaderboard_by_user = {
        row["user_id"]: {"rank": i + 1, "points": row["points"]}
        for i, row in enumerate(ranked)
    }

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "username", "full_name", "final_rank", "final_points",
        "savings_balance", "surcharge_owed", "loan_status", "loan_outstanding",
    ])

    for u in users:
        cur.execute(
            "SELECT COALESCE(SUM(allocated_savings), 0) AS balance FROM savings_transactions WHERE user_id = %s AND status = 'confirmed'",
            (u["id"],),
        )
        savings_balance = cur.fetchone()["balance"]

        cur.execute(
            """
            SELECT s.id, s.amount FROM surcharge_ledger s WHERE s.user_id = %s
            """,
            (u["id"],),
        )
        surcharge_owed = 0
        for s in cur.fetchall():
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) AS cleared FROM surcharge_clearances WHERE surcharge_id = %s",
                (s["id"],),
            )
            surcharge_owed += s["amount"] - cur.fetchone()["cleared"]

        cur.execute(
            "SELECT * FROM loans WHERE user_id = %s ORDER BY requested_at DESC LIMIT 1",
            (u["id"],),
        )
        loan = cur.fetchone()
        loan_status = loan["status"] if loan else "none"
        loan_outstanding = ""
        if loan and loan["status"] == "disbursed":
            interest = (loan["principal"] * loan["interest_rate"] / 100) if loan["interest_rate"] else 0
            total_owed = loan["principal"] + interest
            cur.execute(
                "SELECT COALESCE(SUM(amount), 0) AS repaid FROM loan_repayments WHERE loan_id = %s AND status = 'confirmed'",
                (loan["id"],),
            )
            repaid = cur.fetchone()["repaid"]
            loan_outstanding = total_owed - repaid

        lb = leaderboard_by_user.get(u["id"])

        writer.writerow([
            u["username"],
            u["full_name"] or "",
            lb["rank"] if lb else "",
            lb["points"] if lb else "",
            savings_balance,
            surcharge_owed,
            loan_status,
            loan_outstanding,
        ])

    return output.getvalue()


def close_season(triggered_by_user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            csv_content = _generate_export_csv(cur)

            cur.execute(
                "INSERT INTO season_exports (created_by, csv_content) VALUES (%s, %s) RETURNING id",
                (triggered_by_user_id, csv_content),
            )
            export_id = cur.fetchone()["id"]
            conn.commit()  # export is durable before anything gets wiped
    except Exception as e:
        conn.rollback()
        print(f"Season export failed, aborting close (nothing was wiped): {e}")
        return False, "Export failed -- season was not reset", None

    try:
        now_str = datetime.now(timezone.utc).isoformat()
        with conn.cursor() as cur:
            # Competition data
            cur.execute("DELETE FROM predictions")
            cur.execute("DELETE FROM fixtures")
            cur.execute("DELETE FROM results")
            cur.execute("DELETE FROM matchday_results")
            cur.execute("DELETE FROM leaderboard")
            cur.execute(
                """
                UPDATE matchday_tracker
                SET current_matchday = 0, last_completed_matchday = 0, last_updated = %s
                WHERE id = 1
                """,
                (now_str,),
            )

            # Financial history (append-only ledgers/transactions)
            cur.execute("DELETE FROM surcharge_clearances")
            cur.execute("DELETE FROM surcharge_ledger")
            cur.execute("DELETE FROM savings_transactions")
            cur.execute("DELETE FROM exception_requests")
            cur.execute("DELETE FROM loan_repayments")
            cur.execute("DELETE FROM loan_endorsements")
            cur.execute("DELETE FROM loans")
            cur.execute("DELETE FROM commitment_fee_status")
            cur.execute("DELETE FROM commitment_fee_exceptions")
            cur.execute("UPDATE savings_tracker SET last_processed_week = NULL WHERE id = 1")

            # Rule/config tables -- next season starts with nothing
            # configured, same as a brand-new deployment would.
            cur.execute("DELETE FROM commitment_fee_config")
            cur.execute("DELETE FROM savings_config")
            cur.execute("DELETE FROM loan_config")

            # Admin/treasurer action history. NOTE: season_exports is
            # deliberately NOT cleared here -- see module docstring.
            cur.execute("DELETE FROM audit_log")

            # Users preserved; only the treasurer/secretary flags are
            # stripped (admin untouched) -- roles get reassigned each season.
            cur.execute("UPDATE users SET is_treasurer = 0, is_secretary = 0")

            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Season wipe failed after a successful export (export id {export_id} is safe): {e}")
        return False, f"Wipe failed after export succeeded (export #{export_id} is safe) -- {e}", export_id

    log_action(triggered_by_user_id, 'season_close', 'season_export', export_id)
    return True, None, export_id


def get_export(export_id):
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at, csv_content FROM season_exports WHERE id = %s",
            (export_id,),
        )
        return cur.fetchone()


def list_exports():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.created_at, u.username AS created_by
            FROM season_exports e
            LEFT JOIN users u ON u.id = e.created_by
            ORDER BY e.created_at DESC
            """
        )
        return cur.fetchall()
