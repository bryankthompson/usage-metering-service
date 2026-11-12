"""entitlements.status accepts only what the webhook writes (active, past_due, canceled); the schema refuses the rest."""
import psycopg
import pytest

from app import db


def insert(conn, status):
    conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES ('t1', %s)", (status,))


def test_a_status_outside_the_three_is_refused(conn):
    for status in ("something_new", "trialing", "ACTIVE", ""):
        with pytest.raises(psycopg.errors.CheckViolation):
            insert(conn, status)
        conn.rollback()


@pytest.mark.parametrize("status", ["active", "past_due", "canceled"])
def test_the_three_statuses_are_accepted(conn, status):
    insert(conn, status)


def test_apply_schema_twice_succeeds(conn):
    db.apply_schema(conn)
    db.apply_schema(conn)


def test_apply_schema_adds_the_constraint_to_an_existing_table(conn):
    conn.execute("ALTER TABLE entitlements DROP CONSTRAINT entitlements_status_check")
    conn.commit()
    db.apply_schema(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(conn, "something_new")


def test_apply_schema_fails_loudly_on_a_legacy_row(conn):
    # never committed: the drop and the bad row live in this transaction only, so an interrupted run cannot poison the shared database
    conn.execute("ALTER TABLE entitlements DROP CONSTRAINT entitlements_status_check")
    insert(conn, "something_new")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.apply_schema(conn)
    conn.rollback()
