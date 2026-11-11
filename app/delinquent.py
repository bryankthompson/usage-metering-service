"""Delinquent accounts report: who is past_due or canceled, and whether the edge already denies them.

Read-only. `python -m app.delinquent [--json] [--fail-on-expired] [--at ISO]`. The state rule is the edge's
own (`edge.in_grace`), and the clock is `edge.now`, so a test can fix the time.
"""
import argparse
import json
import sys
from datetime import datetime

from app import db, edge

# Denied states come first in the report, then in_grace.
STATE_ORDER = {"grace_expired": 0, "past_due_no_grace": 1, "canceled": 2, "in_grace": 3}
DENIED_WHILE_PAST_DUE = ("grace_expired", "past_due_no_grace")


def classify(status: str, grace_until: datetime | None, at: datetime) -> tuple[str, int | None]:
    """Return (state, whole hours): hours left for in_grace, hours ago for grace_expired, else None."""
    if status == "canceled":
        return "canceled", None
    if grace_until is None:
        return "past_due_no_grace", None
    if edge.in_grace(grace_until, at):
        return "in_grace", int((grace_until - at).total_seconds() // 3600)
    return "grace_expired", int((at - grace_until).total_seconds() // 3600)


def report(conn, at: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT tenant_id, status, payment_failed_at, grace_until FROM entitlements "
        "WHERE status IN ('past_due', 'canceled')").fetchall()
    out = []
    for row in rows:
        state, hours = classify(row["status"], row["grace_until"], at)
        out.append({
            "tenant_id": row["tenant_id"], "status": row["status"], "state": state,
            "payment_failed_at": row["payment_failed_at"], "grace_until": row["grace_until"],
            "hours_left": hours if state == "in_grace" else None,
            "hours_ago": hours if state == "grace_expired" else None,
        })
    out.sort(key=lambda r: (STATE_ORDER[r["state"]], r["grace_until"] if r["state"] == "in_grace" else 0, r["tenant_id"]))
    return out


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware_iso(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO time: {text!r}") from None
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError("needs a time zone, for example 2026-11-14T00:00:00+00:00")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.delinquent", description="List past_due and canceled tenants.")
    parser.add_argument("--json", action="store_true", help="one JSON object per line")
    parser.add_argument("--fail-on-expired", action="store_true",
                        help="exit 1 when any past_due tenant is denied (grace expired or no grace recorded)")
    parser.add_argument("--at", type=_aware_iso, metavar="ISO", help="evaluate at this time (with a zone) instead of now")
    args = parser.parse_args(argv)
    at = args.at or edge.now()
    conn = db.connect(statement_timeout_ms=0)
    try:
        rows = report(conn, at)
    finally:
        conn.close()
    for row in rows:
        row = {**row, "payment_failed_at": _iso(row["payment_failed_at"]), "grace_until": _iso(row["grace_until"])}
        if args.json:
            print(json.dumps(row))
        else:
            detail = (f"{row['hours_left']}h left" if row["hours_left"] is not None
                      else f"{row['hours_ago']}h ago" if row["hours_ago"] is not None else "")
            print(f"{row['tenant_id']:<20} {row['status']:<9} {row['state']:<18} {detail:<9} "
                  f"failed={row['payment_failed_at'] or '-'} grace_until={row['grace_until'] or '-'}")
    if not rows and not args.json:
        print("no delinquent tenants")
    return 1 if args.fail_on_expired and any(r["state"] in DENIED_WHILE_PAST_DUE for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
