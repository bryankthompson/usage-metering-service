"""Postgres access: plain SQL through psycopg. Each caller opens and closes its own connection."""
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "db"
DEFAULT_URL = "postgresql://metering:metering@localhost:5432/metering"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


CONNECT_TIMEOUT_S = 5
REQUEST_STATEMENT_TIMEOUT_MS = 15_000


def connect(url: str | None = None, *, statement_timeout_ms: int | None = None) -> psycopg.Connection:
    """Open a connection that gives up after CONNECT_TIMEOUT_S, so a database that stops answering is a 503, not a hang.

    The default is the request path's: statements are cancelled after REQUEST_STATEMENT_TIMEOUT_MS (psycopg raises
    QueryCanceled, an OperationalError, which the API answers with 503). The rollup close, reconcile and delinquent
    pass `statement_timeout_ms=0` (no limit): a large hour legitimately takes longer.
    """
    if statement_timeout_ms is None:
        statement_timeout_ms = REQUEST_STATEMENT_TIMEOUT_MS
    return psycopg.connect(url or database_url(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_S,
                           options=f"-c statement_timeout={statement_timeout_ms}")


def apply_schema(conn: psycopg.Connection) -> None:
    """Run db/*.sql in filename order. Every file is repeatable."""
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        conn.execute(path.read_text())
    conn.commit()


def get_conn() -> Iterator[psycopg.Connection]:
    """FastAPI dependency: a connection that is closed when the request ends."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
