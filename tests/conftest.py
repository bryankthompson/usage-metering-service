import os

import pytest

from app import db

# This process's pool sessions carry their own name, so a dev server (same app, same database) is not counted as ours.
# Set before any pool opens; tests/test_pool.py reads it at import for its CLEAN settings.
db.APPLICATION_NAME = f"usage-metering-api-test-{os.getpid()}"


def own_pool_sessions(conn, *, state_like="%"):
    """How many sessions this test process's request pool holds on the database (in a state matching state_like).
    The commit first: pg_stat_activity is a snapshot kept until the transaction ends, so without it every poll repeats the first answer."""
    conn.commit()
    return conn.execute(
        "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s AND pid <> pg_backend_pid()"
        " AND state LIKE %s",
        (db.APPLICATION_NAME, state_like),
    ).fetchone()["n"]


def database_url_problem(environ):
    """The message to stop with when DATABASE_URL is unset or empty, else None."""
    if environ.get("DATABASE_URL"):
        return None
    return (
        "DATABASE_URL is not set: the tests TRUNCATE the database they connect to, and the default is "
        "localhost:5432 (a live compose database).\n"
        "Run `scripts/test` (own database on port 5455), or set DATABASE_URL yourself."
    )


def pytest_sessionstart(session):
    problem = database_url_problem(os.environ)
    if problem:
        pytest.exit(problem, returncode=2)


@pytest.fixture(autouse=True)
def fresh_request_pool():
    """The request pool is made on first use from DATABASE_URL and DB_POOL_*, which tests change: start and end each test without one."""
    db.close_pool()
    yield
    db.close_pool()


@pytest.fixture
def conn():
    """A connection to an empty, fully migrated test database. Fails loudly if Postgres is unreachable."""
    connection = db.connect()
    db.apply_schema(connection)
    tables = [
        r["tablename"]
        for r in connection.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    ]
    connection.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()
