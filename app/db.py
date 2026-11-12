"""Postgres access: plain SQL through psycopg. The API's request path borrows connections from a pool (`request_connection`);
the rollup, reconcile, delinquent and mirror jobs and the tests open and close their own (`connect`)."""

import atexit
import logging
import math
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "db"
DEFAULT_URL = "postgresql://metering:metering@localhost:5432/metering"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


CONNECT_TIMEOUT_S = 5
REQUEST_STATEMENT_TIMEOUT_MS = 15_000


def connect(url: str | None = None, *, statement_timeout_ms: int | None = None) -> psycopg.Connection:
    """Open a connection that gives up after CONNECT_TIMEOUT_S, so a database that stops answering is a 503, not a hang.

    The default is the request path's: statements are cancelled after REQUEST_STATEMENT_TIMEOUT_MS (psycopg raises
    QueryCanceled, which the API's app-wide handler answers with 504 "query timed out"). The rollup close, reconcile and delinquent
    pass `statement_timeout_ms=0` (no limit): a large hour legitimately takes longer.

    The session time zone is pinned to UTC for every connection, whatever PGTZ, the URL or the server says: the app reads
    datetimes back and prints their clock hour or offset (reconcile, charges, delinquent), and a zone west of UTC made
    those name the wrong hour and made a year-one date a 500.
    """
    if statement_timeout_ms is None:
        statement_timeout_ms = REQUEST_STATEMENT_TIMEOUT_MS
    conn = psycopg.connect(url or database_url(), row_factory=dict_row, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        _session_setup(conn, statement_timeout_ms)
    except BaseException:
        conn.close()
        raise
    return conn


def _session_setup(conn: psycopg.Connection, statement_timeout_ms: int) -> None:
    # A session SET, not the `options` keyword: that would replace any options in the URL or PGOPTIONS.
    conn.execute(
        "SELECT set_config('statement_timeout', %s, false), set_config('TimeZone', 'UTC', false)",
        (str(int(statement_timeout_ms)),),
    )
    conn.commit()


# --- the request path's pool ------------------------------------------------------------------------------------------

# env DB_POOL_MAX: today's worst case, one connection for each of the 40 worker threads (anyio's default limit), per process.
# env DB_POOL_TIMEOUT (seconds a request waits for a free connection) defaults to CONNECT_TIMEOUT_S.
DEFAULT_POOL_MAX = 40
POOL_MIN = 2
APPLICATION_NAME = "usage-metering-api"  # in pg_stat_activity: which sessions are the request path's pool

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
_holding = threading.local()  # whether this thread has a pooled connection checked out


def _env_number(name: str, default: float, kind: type) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}={raw!r} must be a finite number greater than 0")
    return value


def _configure_pooled(conn: psycopg.Connection) -> None:
    """Runs once on each new physical connection. Ends in a commit: the pool wants it idle, and autocommit stays False,
    because every `pg_advisory_xact_*` and transaction-local `set_config` the request path uses needs a transaction."""
    _session_setup(conn, REQUEST_STATEMENT_TIMEOUT_MS)


def _request_pool() -> ConnectionPool:
    """The per-process pool, created on first use so importing the app (or starting it with the database down) opens nothing."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                size = int(_env_number("DB_POOL_MAX", DEFAULT_POOL_MAX, int))
                pool = ConnectionPool(
                    database_url(),
                    min_size=min(POOL_MIN, size),
                    max_size=size,
                    timeout=_env_number("DB_POOL_TIMEOUT", CONNECT_TIMEOUT_S, float),
                    # `prepare_threshold=None`: a connection that lives for hours would otherwise switch to prepared statements
                    # after five uses of a query, and a generic plan for the range queries; a fresh connection never did.
                    kwargs={
                        "row_factory": dict_row,
                        "connect_timeout": CONNECT_TIMEOUT_S,
                        "prepare_threshold": None,
                        "application_name": APPLICATION_NAME,
                    },
                    configure=_configure_pooled,
                    check=ConnectionPool.check_connection,  # a connection that died while idle is replaced, not handed out
                    open=False,
                )
                pool.open(wait=False)  # a database that is down now does not stop the app starting
                _pool = pool
    return _pool


@contextmanager
def request_connection():
    """Borrow a pooled connection for one short unit of work: `with db.request_connection() as conn:`.

    It has the request path's session (REQUEST_STATEMENT_TIMEOUT_MS, UTC, autocommit off), commits when the block ends and
    rolls back when it raises, then goes back to the pool, so it behaves as `with connect() as conn` did. When no connection
    comes free within DB_POOL_TIMEOUT seconds (or the database cannot be reached) it raises a psycopg OperationalError
    (PoolTimeout), which is a 503 like every other database that does not answer.

    Take it inside the handler and give it back before returning, never across a dependency: a request holding a connection
    while it waits for a worker thread is what used up Postgres's connection slots, and with a bounded pool it would deadlock.
    A thread that asks for a second one while it holds the first would wait for PoolTimeout; that raises at once instead.
    """
    if getattr(_holding, "yes", False):
        raise RuntimeError("this thread already holds a pooled connection: give it back before taking another")
    pool = _request_pool()
    try:
        with pool.connection() as conn:
            _holding.yes = True
            try:
                yield conn
            finally:
                _holding.yes = False
    except PoolTimeout:
        log.error("no pooled connection came free: %s", pool.get_stats())
        raise


def close_pool() -> None:
    """Close the request pool (it is opened again on the next use). At exit, and in tests that change DATABASE_URL or DB_POOL_MAX."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.close()


atexit.register(close_pool)


LOCK_WAIT = "2s"  # how long a request waits for a lock before it answers 503; never used on the rollup (closing) side


def limit_lock_wait(conn: psycopg.Connection) -> None:
    """For the rest of the current transaction, a wait for any lock longer than LOCK_WAIT raises LockNotAvailable.

    A request that waits for a held lock pins a worker thread and a connection; about 40 of them used up the worker pool, so
    every other database route waited with them (`/healthz` is async and keeps answering). The error is a psycopg.OperationalError, so it is answered 503 with Retry-After, and the same
    request can be resent: nothing of the failed attempt is committed. Same as SET LOCAL lock_timeout.
    """
    conn.execute("SELECT set_config('lock_timeout', %s, true)", (LOCK_WAIT,))


def apply_schema(conn: psycopg.Connection) -> None:
    """Run db/*.sql in filename order. Every file is repeatable."""
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        conn.execute(path.read_text())
    conn.commit()
