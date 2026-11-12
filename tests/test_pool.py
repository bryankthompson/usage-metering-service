"""The request path borrows connections from a bounded pool (db.request_connection) instead of opening one per use.

What must hold: Postgres never sees more request-path sessions than the cap; a borrowed session is always the request
path's (statement timeout, UTC, autocommit off, no state left behind); nothing deadlocks when requests outnumber the
slots; a database that stops and comes back, or a killed backend, costs requests only while it is gone; and when the pool
cannot give a connection the edge denies (503) instead of admitting."""
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import PoolTimeout, TooManyRequests

from app import db, edge
from app.edge import require_entitlement
from app.main import app
from tests.test_attack_ingest import event

DEADLINE_S = 60
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
RANGE = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=3)).isoformat()}


def pool_settings(monkeypatch, size, timeout=None):
    monkeypatch.setenv("DB_POOL_MAX", str(size))
    if timeout is not None:
        monkeypatch.setenv("DB_POOL_TIMEOUT", str(timeout))


@pytest.fixture
def api(conn):
    """The real app behind the real edge (so a request takes two checkouts), tenant t1 entitled."""
    conn.execute("INSERT INTO entitlements (tenant_id, status) VALUES ('t1', 'active')")
    conn.commit()
    return TestClient(app, raise_server_exceptions=False)


def post(client, key, **changes):
    return client.post("/v1/usage", json=event(idempotency_key=key, **changes), headers={"X-Tenant-Id": "t1"})


def stored(conn) -> int:
    conn.commit()
    return conn.execute("SELECT count(*) AS n FROM usage_events").fetchone()["n"]


def own_pids(*connections) -> list[int]:
    return [c.info.backend_pid for c in connections]


def backends_other_than(observer, *mine) -> int:
    return observer.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                            " AND NOT (pid = ANY(%s))", (own_pids(observer, *mine),)).fetchone()["n"]


# --- the library's error classes, on the installed version -----------------------------------------------------------

@pytest.mark.parametrize("error", [PoolTimeout, TooManyRequests])
def test_the_pools_refusals_are_operational_errors_so_they_are_503(error):
    assert issubclass(error, psycopg.OperationalError)


# --- configuration ---------------------------------------------------------------------------------------------------

def test_the_defaults_follow_the_worker_thread_limit_and_the_connect_timeout():
    pool = db._request_pool()
    assert (pool.max_size, pool.min_size, pool.timeout) == (40, 2, db.CONNECT_TIMEOUT_S)


def test_the_cap_and_the_wait_come_from_the_environment(monkeypatch):
    pool_settings(monkeypatch, 7, 1.5)
    pool = db._request_pool()
    assert (pool.max_size, pool.timeout) == (7, 1.5)


def test_a_cap_of_one_still_starts(monkeypatch):
    pool_settings(monkeypatch, 1)
    assert (db._request_pool().min_size, db._request_pool().max_size) == (1, 1)


@pytest.mark.parametrize(("name", "value"), [("DB_POOL_MAX", "many"), ("DB_POOL_MAX", "0"), ("DB_POOL_MAX", "-3"),
                                             ("DB_POOL_TIMEOUT", "soon"), ("DB_POOL_TIMEOUT", "0")])
def test_a_bad_setting_fails_loudly_and_names_itself(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        with db.request_connection():
            pytest.fail("a connection was given with a setting that makes no sense")


def test_closing_the_pool_twice_is_fine_and_the_next_use_opens_a_new_one():
    first = db._request_pool()
    db.close_pool()
    db.close_pool()
    assert first.closed and db._request_pool() is not first


def test_the_app_starts_and_the_pool_is_made_with_the_database_down(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://metering:metering@localhost:1/metering")   # nothing listens on port 1
    pool_settings(monkeypatch, 3, 0.5)
    started = time.monotonic()
    with pytest.raises(PoolTimeout):
        with db.request_connection():
            pytest.fail("there is no database")
    assert time.monotonic() - started < 5


# --- (a) the cap -----------------------------------------------------------------------------------------------------

def test_200_concurrent_requests_never_open_more_sessions_than_the_cap(api, conn, monkeypatch):
    cap = 6
    pool_settings(monkeypatch, cap)
    sampler = db.connect()
    sampler.autocommit = True                       # so every poll sees a fresh pg_stat_activity
    peak, done = [0], threading.Event()

    def sample():
        while not done.is_set():
            peak[0] = max(peak[0], backends_other_than(sampler, conn))
            time.sleep(0.002)

    watcher = threading.Thread(target=sample)
    watcher.start()
    try:
        try:
            with TestClient(app, raise_server_exceptions=False) as shared, ThreadPoolExecutor(max_workers=60) as senders:
                sends = [senders.submit(post, shared, f"k{i}") for i in range(200)]
                statuses = [s.result(timeout=DEADLINE_S).status_code for s in sends]
        finally:
            done.set()
            watcher.join()
        pooled = sampler.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s",
                                 (db.APPLICATION_NAME,)).fetchone()["n"]
    finally:
        sampler.close()
    assert statuses == [201] * 200
    assert 1 <= peak[0] <= cap, f"{peak[0]} sessions at the peak with a cap of {cap}"
    assert pooled <= cap
    assert stored(conn) == 200


# --- (b) a borrowed session is clean, every time ---------------------------------------------------------------------

def session_facts(c):
    """What the request path relies on, read inside the session that was borrowed."""
    return {
        "autocommit": c.autocommit,
        "prepare_threshold": c.prepare_threshold,     # None: a long-lived connection never switches to generic plans
        "statement_timeout": c.execute("SHOW statement_timeout").fetchone()["statement_timeout"],
        "TimeZone": c.execute("SHOW TimeZone").fetchone()["TimeZone"],
        "lock_timeout": c.execute("SHOW lock_timeout").fetchone()["lock_timeout"],
        "application_name": c.execute("SHOW application_name").fetchone()["application_name"],
    }


CLEAN = {"autocommit": False, "prepare_threshold": None, "statement_timeout": "15s", "TimeZone": "UTC", "lock_timeout": "0",
         "application_name": db.APPLICATION_NAME}


def test_every_borrow_is_the_request_sessions_even_after_a_borrower_failed(conn, monkeypatch):
    pool_settings(monkeypatch, 1)                   # one physical connection, so every borrow is the same session
    pids, seen = [], []

    def borrow(fail=False):
        with db.request_connection() as c:
            assert c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE      # nothing open when it is handed out
            pids.append(c.info.backend_pid)
            seen.append(session_facts(c))
            db.limit_lock_wait(c)                                                      # the request path's transaction state
            c.execute("SELECT pg_advisory_xact_lock(1, 1)")
            if fail:
                raise ZeroDivisionError

    borrow()
    with pytest.raises(ZeroDivisionError):
        borrow(fail=True)
    borrow()
    assert len(set(pids)) == 1 and seen == [CLEAN] * 3


def test_after_a_mix_of_requests_the_session_holds_no_state_and_no_lock(api, conn, monkeypatch):
    pool_settings(monkeypatch, 1)
    assert post(api, "a").status_code == 201
    assert post(api, "a").status_code == 200                                           # a duplicate
    assert post(api, "a", tokens_in=99).status_code == 409                             # a conflict
    assert post(api, "b", tenant_id="t2").status_code == 403                           # refused after the edge
    assert api.get("/v1/tenants/t1/charges", params=RANGE, headers={"X-Tenant-Id": "t1"}).status_code == 200
    assert api.get("/v1/tenants/t1/charges", params={"start": "x"}, headers={"X-Tenant-Id": "t1"}).status_code == 422
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'").fetchone()["n"] == 0
    with db.request_connection() as c:
        assert session_facts(c) == CLEAN


# --- (c) no deadlock when requests outnumber the slots ---------------------------------------------------------------

def test_more_concurrent_requests_than_pool_slots_all_finish(api, conn, monkeypatch):
    pool_settings(monkeypatch, 2, 3)
    headers = {"X-Tenant-Id": "t1"}
    with TestClient(app, raise_server_exceptions=False) as shared, ThreadPoolExecutor(max_workers=40) as senders:
        sends = [senders.submit(post, shared, f"k{i}") for i in range(30)]
        sends += [senders.submit(shared.get, "/v1/tenants/t1/charges", params=RANGE, headers=headers) for _ in range(10)]
        statuses = [s.result(timeout=DEADLINE_S).status_code for s in sends]
    assert statuses == [201] * 30 + [200] * 10
    assert stored(conn) == 30


def test_a_thread_that_asks_for_a_second_connection_is_refused_at_once_not_after_the_timeout(monkeypatch):
    pool_settings(monkeypatch, 1, 30)
    started = time.monotonic()
    with db.request_connection():
        with pytest.raises(RuntimeError, match="already holds"):
            with db.request_connection():
                pytest.fail("a second connection was given to a thread that holds one")
    assert time.monotonic() - started < 5
    with db.request_connection() as c:                                                 # and the first was given back
        assert c.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_a_failed_borrow_does_not_leave_the_thread_marked_as_holding_one(monkeypatch):
    pool_settings(monkeypatch, 1)
    with pytest.raises(ZeroDivisionError):
        with db.request_connection():
            raise ZeroDivisionError
    with db.request_connection():
        pass


# --- (d) the database goes away and comes back -----------------------------------------------------------------------

class Proxy:
    """A TCP forwarder to the test database that can be stopped (every connection cut, nothing listening) and started again
    on the same port: what a database restart looks like from the api."""

    def __init__(self, target: dict):
        self.target = (target.get("host") or "localhost", int(target.get("port") or 5432))
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.sockets, self.listener, self.running = [], None, False

    def start(self):
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.listener.listen(64)
        self.running = True
        self.acceptor = threading.Thread(target=self._accept, args=(self.listener,), daemon=True)
        self.acceptor.start()

    def _accept(self, listener):
        while True:
            try:
                client, _ = listener.accept()
                server = socket.create_connection(self.target)
            except OSError:
                return
            self.sockets += [client, server]
            for src, dst in ((client, server), (server, client)):
                threading.Thread(target=self._pipe, args=(src, dst), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def stop(self):
        self.running = False
        try:
            self.listener.shutdown(socket.SHUT_RDWR)      # close() alone leaves a blocked accept() holding the port
        except OSError:
            pass
        self.listener.close()
        self.acceptor.join(timeout=5)
        sockets, self.sockets = self.sockets, []
        for s in sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        assert not self.acceptor.is_alive(), "the proxy's accept thread did not stop"      # after the cleanup, which always runs

    @property
    def url(self):
        return f"postgresql://metering:metering@127.0.0.1:{self.port}/{conninfo_to_dict(db.database_url())['dbname']}"


def test_a_database_that_stops_and_starts_is_503_while_down_and_201_after_with_no_restart(api, conn, monkeypatch):
    proxy = Proxy(conninfo_to_dict(db.database_url()))
    proxy.start()
    try:
        monkeypatch.setenv("DATABASE_URL", proxy.url)
        pool_settings(monkeypatch, 4, 1)
        assert post(api, "before").status_code == 201
        proxy.stop()
        down = post(api, "while-down")
        assert (down.status_code, down.json()) == (503, {"detail": "entitlement check unavailable"})
        assert stored(conn) == 1
        proxy.start()
        assert post(api, "while-down").status_code == 201                              # the same key, the same process
        assert post(api, "after").status_code == 201
        assert stored(conn) == 3
    finally:
        if proxy.running:
            proxy.stop()


# --- (e) a killed backend is replaced --------------------------------------------------------------------------------

def test_a_backend_killed_while_idle_in_the_pool_is_replaced_and_the_next_request_succeeds(api, conn, monkeypatch):
    pool_settings(monkeypatch, 3)
    assert post(api, "warm").status_code == 201
    killed = conn.execute("SELECT count(pg_terminate_backend(pid)) AS n FROM pg_stat_activity WHERE application_name = %s",
                          (db.APPLICATION_NAME,)).fetchone()["n"]
    conn.commit()
    assert killed >= 1
    time.sleep(0.2)                                                                    # let the backends really go
    assert post(api, "after-kill").status_code == 201
    assert stored(conn) == 2


# --- (f) a pool that cannot give a connection is 503, and the edge denies ---------------------------------------------

def test_a_pool_with_no_free_connection_makes_the_edge_deny_and_nothing_is_stored(api, conn, monkeypatch, caplog):
    pool_settings(monkeypatch, 1, 0.5)
    with db.request_connection():                                                      # the only slot, taken
        with ThreadPoolExecutor(max_workers=1) as one:
            resp = one.submit(post, api, "k1").result(timeout=DEADLINE_S)
    assert (resp.status_code, resp.json()) == (503, {"detail": "entitlement check unavailable"})
    assert stored(conn) == 0
    assert "no pooled connection came free" in caplog.text and "pool_size" in caplog.text


def test_a_pool_with_no_free_connection_after_the_edge_admitted_is_503_database_unavailable(conn, monkeypatch):
    pool_settings(monkeypatch, 1, 0.5)
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    try:
        with db.request_connection():
            with ThreadPoolExecutor(max_workers=1) as one:
                resp = one.submit(post, TestClient(app, raise_server_exceptions=False), "k1").result(timeout=DEADLINE_S)
    finally:
        app.dependency_overrides.clear()
    assert (resp.status_code, resp.json(), resp.headers["retry-after"]) == (503, {"detail": "database unavailable"}, "1")
    assert stored(conn) == 0


@pytest.mark.parametrize("error", [PoolTimeout("no connection"), TooManyRequests("too many waiting")], ids=lambda e: type(e).__name__)
def test_both_pool_refusals_are_503_at_the_edge_and_in_the_handler(api, conn, monkeypatch, error):
    def refuse():
        raise error

    monkeypatch.setattr(edge, "request_connection", refuse)
    at_edge = post(api, "k1")
    assert (at_edge.status_code, at_edge.json()) == (503, {"detail": "entitlement check unavailable"})
    monkeypatch.undo()

    monkeypatch.setattr(db, "request_connection", refuse)
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    try:
        in_handler = post(TestClient(app, raise_server_exceptions=False), "k1")
    finally:
        app.dependency_overrides.clear()
    assert (in_handler.status_code, in_handler.json(), in_handler.headers["retry-after"]) == (
        503, {"detail": "database unavailable"}, "1")
    assert stored(conn) == 0

