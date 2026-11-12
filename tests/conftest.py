import os

import pytest

from app import db


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
