"""POST /v1/usage refuses a body over 64 KiB with 413 before it is parsed and before the edge runs (RT-security-2: a 600 MB
body with no credentials took the API from 56 MiB to 915 MiB). The webhook keeps its own 1 MiB cap."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.edge import require_entitlement
from app.bodylimit import route_path
from app.main import app
from tests.test_attack_ingest import count, event

CAP = 64 * 1024
JSON = {"content-type": "application/json"}


def body_of(size: int) -> bytes:
    """A valid usage event padded with trailing spaces to exactly `size` bytes."""
    raw = json.dumps(event()).encode()
    return raw + b" " * (size - len(raw))


def chunks(data: bytes, size: int = 4096):
    for start in range(0, len(data), size):
        yield data[start : start + size]


@pytest.fixture
def admitted(conn):
    app.dependency_overrides[require_entitlement] = lambda: "t1"
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_an_oversize_body_with_no_credentials_is_413_not_401(conn):
    r = TestClient(app).post("/v1/usage", content=body_of(CAP + 1), headers=JSON)
    assert (r.status_code, r.json()) == (413, {"detail": "request body too large"})


def test_an_oversize_chunked_body_with_no_content_length_is_413(conn):
    r = TestClient(app).post("/v1/usage", content=chunks(body_of(CAP * 4)), headers=JSON)
    assert r.status_code == 413


def test_an_oversize_body_never_reaches_the_handler(admitted, conn):
    assert admitted.post("/v1/usage", content=body_of(CAP + 1), headers=JSON).status_code == 413
    assert admitted.post("/v1/usage", content=chunks(body_of(CAP + 1)), headers=JSON).status_code == 413
    assert count(conn) == 0


def test_a_body_of_exactly_the_cap_is_still_accepted(admitted, conn):
    assert admitted.post("/v1/usage", content=body_of(CAP), headers=JSON).status_code == 201
    assert (
        admitted.post("/v1/usage", content=chunks(body_of(CAP)), headers=JSON).status_code == 200
    )  # same key: a duplicate
    assert count(conn) == 1


def test_a_normal_event_is_still_201(admitted, conn):
    assert admitted.post("/v1/usage", json=event()).status_code == 201 and count(conn) == 1


def test_an_unauthenticated_small_body_is_still_401_from_the_edge(conn):
    r = TestClient(app).post("/v1/usage", json=event())
    assert r.status_code == 401


def test_a_lying_content_length_cannot_get_past_the_stream_count(admitted, conn):
    # Content-Length says 10 but the stream carries more: the count of what arrives decides.
    r = admitted.post("/v1/usage", content=chunks(body_of(CAP * 2)), headers={**JSON, "content-length": "10"})
    assert r.status_code in (400, 413) and count(conn) == 0


def test_the_stream_is_read_only_up_to_the_cap_not_to_its_end():
    read = {"chunks": 0}

    async def receive():
        read["chunks"] += 1
        return {"type": "http.request", "body": b"x" * 8192, "more_body": read["chunks"] < 2000}  # a 16 MB body

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/usage",
        "raw_path": b"/v1/usage",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json")],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    asyncio.run(app(scope, receive, send))
    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 413
    assert read["chunks"] <= CAP // 8192 + 2, f"read {read['chunks']} chunks of a body that was over the cap after 8"


def test_the_webhook_keeps_its_own_larger_cap(conn):
    r = TestClient(app).post("/v1/stripe/webhook", content=b"x" * (CAP * 3), headers={"Stripe-Signature": "t=1,v1=00"})
    assert r.status_code != 413  # 400 (bad signature) or 503 (no secret), not the usage cap
    assert TestClient(app).post("/v1/stripe/webhook", content=b"x" * (1024 * 1024 + 1)).status_code == 413


def test_a_declared_content_length_over_the_cap_is_refused_without_reading_a_byte():
    async def receive():
        raise AssertionError("the body was read although Content-Length already said it was too large")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/v1/usage",
        "raw_path": b"/v1/usage",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(CAP + 1).encode())],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    asyncio.run(app(scope, receive, send))
    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 413


def test_the_cap_still_applies_behind_a_proxy_that_mounts_the_app_under_a_prefix():
    # Behind a proxy with a path prefix the server sets root_path="/m" and path="/m/v1/usage"; the route is still /v1/usage.
    async def receive():
        raise AssertionError("the body was read although Content-Length already said it was too large")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/m/v1/usage",
        "raw_path": b"/m/v1/usage",
        "query_string": b"",
        "root_path": "/m",
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(CAP + 1).encode())],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    asyncio.run(app(scope, receive, send))
    assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 413


def status_for(path: str, root_path: str) -> int:
    async def receive():
        raise AssertionError("the body was read although Content-Length already said it was too large")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": root_path,
        "scheme": "http",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(CAP + 1).encode())],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_the_cap_applies_to_an_unprefixed_path_when_a_root_path_is_set():
    assert status_for("/v1/usage", "/m") == 413


@pytest.mark.parametrize(
    "path, root_path, expected",
    [
        ("/m/v1/usage", "/m", "/v1/usage"),
        ("/v1/usage", "", "/v1/usage"),
        ("/v1/usage", "/m", "/v1/usage"),  # the path does not start with root_path
        ("/m", "/m", ""),
        ("/mx/v1/usage", "/m", "/mx/v1/usage"),  # root_path matches only at a "/" boundary
    ],
)
def test_route_path_follows_starlettes_rule(path, root_path, expected):
    assert route_path({"path": path, "root_path": root_path}) == expected
