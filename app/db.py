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


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url(), row_factory=dict_row)


LOCK_WAIT = "2s"        # how long a request waits for a lock before it answers 503; never used on the rollup (closing) side


def limit_lock_wait(conn: psycopg.Connection) -> None:
    """For the rest of the current transaction, a wait for any lock longer than LOCK_WAIT raises LockNotAvailable.

    A request that waits for a held lock pins a worker thread and a connection; about 40 of them hang the whole API
    (healthz included). The error is a psycopg.OperationalError, so it is answered 503 with Retry-After, and the same
    request can be resent: nothing of the failed attempt is committed. Same as SET LOCAL lock_timeout.
    """
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (LOCK_WAIT,))


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
