import pytest

from app import db


@pytest.fixture
def conn():
    """A connection to an empty, fully migrated test database. Fails loudly if Postgres is unreachable."""
    connection = db.connect()
    db.apply_schema(connection)
    tables = [r["tablename"] for r in connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
    connection.execute("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    connection.close()
