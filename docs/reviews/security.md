# Red team: security lens (2026-11-10)

> Review report as written at the time. File:line citations refer to the SHA named in the report, not to today's tree.

Checkout: origin/main (da520b6). Live stack: own compose project `rt-security` (API 18401, DB 15401), removed with `down -v`.
Attack scripts (not committed): seed.py, sig.py
Summary: 0 blockers, 2 major, 2 minor, 2 notes.

## RT-security-1 — MAJOR — a canceled tenant is reactivated by a same-second, late-delivered subscription event
File: app/webhooks.py:130 (`created < row["last_event_at"]` is strict) and docs/billing.md ("equal timestamps apply in arrival order")
- Stripe `created` has one-second resolution and Stripe does not guarantee delivery order. A `customer.subscription.deleted`
  followed by a `customer.subscription.updated` (status active) with the same `created` second is applied in arrival order, so the
  late event wins and access returns after the cancel. The docs list the tie rule but not that it can undo a cancel. Cancel -> access = "service without paying".
- Repro (signed with the compose secret; seed.py `post`):
  created=T: updated active (processed); deleted (processed) -> edge 402; updated active, new event id, same T -> `processed`, edge 200.
  Observed output: `after delete: 402` / `after same-second stale active: 200`.
- Smallest fix: on `created == last_event_at`, never let a non-canceling event overwrite a stored `canceled` (treat the tie as stale when row status is canceled and the event is not itself a delete).

## RT-security-2 — MAJOR — POST /v1/usage reads the whole body into memory before the edge answers 401
File: app/ingest.py:121 (body model is a parameter, `require_entitlement` a dependency); no size cap outside app/webhooks.py:28
- FastAPI reads and JSON-parses the body before it resolves the entitlement dependency, so a caller with no tenant header (or any
  invented one) makes the API buffer the full body. The webhook route was capped for this exact reason (comment at webhooks.py:30); the usage route was not.
- Repro: 600 MB body of `{"x":"aaaa...`, no auth:
  `curl -s -o /dev/null -w "%{http_code} %{time_total}s" -H 'content-type: application/json' --data-binary @big.json localhost:18401/v1/usage`
  -> `401 1.54s`; `docker stats` during the request: `914.9MiB` (idle 56 MiB). Roughly 1.5x the body, per concurrent request, no container memory limit in compose.yaml. An outsider can OOM the API.
- Smallest fix: a body-size middleware (reject by Content-Length and count the stream, as `raw_body` does) on all routes, e.g. 64 KiB for /v1/usage.

## RT-security-3 — MINOR — the webhook secret in compose.yaml is public and nothing refuses it
File: compose.yaml:22, scripts/demo.sh:8, scripts/flood.py:15, app/webhooks.py:49
- `whsec_test_only_not_a_real_secret` is in the repo and in git history. Anyone who can reach a deployment that kept it can sign a
  subscription event and make any tenant `active` (I did this against the live stack: tenant `mallory` -> 200). It is fake on purpose and the port is loopback only, so this only matters if the file is reused.
- Smallest fix: `verify()` logs an ERROR (or refuses) when the secret equals the placeholder unless an explicit `ALLOW_TEST_SECRET=1` is set; or compose uses `${STRIPE_WEBHOOK_SECRET:?set it}`.

## RT-security-4 — MINOR — container runs as root, base images unpinned
File: Dockerfile:1-2,9
- `ghcr.io/astral-sh/uv:latest` and `python:3.12-slim` are floating tags and no `USER` is set, so uvicorn runs as root in the container. A build next month can pull different code under the same lockfile.
- Fix: pin the uv image to a version/digest, add `RUN useradd -r app` and `USER app`.

## NOTE
- RT-security-5: `/docs`, `/redoc` and `/openapi.json` are served unauthenticated (FastAPI defaults); fine locally, remove for anything exposed.
- RT-security-6: stripe's `verify_header` only rejects timestamps that are too old (stripe/_webhook.py:189); a signature with `t` 10 days in the future is accepted (`future t (+10 days) signed` was processed/duplicate in sig.py). Needs a valid signature, so only a Stripe clock fault matters.

## What I tried that held
- Cross-tenant on charges: path `bob`, `Bob`, `bob%20`, `%62ob`, `alice/../bob`, `bob%2f` with header alice -> 403/404. Header `alice ` -> alice, `ALICE`, zero-width-space variant -> 402 (no alias). Body tenant != header tenant -> 403.
- Signatures: no header, empty header, wrong secret, signature made with an empty secret, `v0` only, bad `t=1` first, body altered, trailing byte, 10-minute-old `t`, replay of the same event id (-> `duplicate`). All 400 / duplicate. Non-ASCII signature is a 400 (code path at webhooks.py:52).
- 100k-deep JSON on /v1/usage and /v1/stripe/webhook -> 400, no 500. 100,000-digit integer -> 400 in 1 ms. Webhook body cap (413) works via stream.
- SQL injection: `'; drop table x;--` as model and in the key, `'` in the charges query string: stored/validated as data; the f-string at webhooks.py:109 takes only the literals `tenant_id` and `stripe_customer_id`; reconcile.py and delinquent.py pass parameters only. Not injectable.
- Log injection: a key with ESC and newline is logged with %r (`'k\x1b[31mRED\nFAKE LINE'`). Tenant ids are logged unquoted at ingest.py:132/141 (`tenant=%s`), but the webhook refuses control characters < 0x20 and 0x7F in tenant ids (C1/U+2028 would pass, only an operator can set them).
- Charges range extremes (`0001-01-01+23:59` to `9999-12-31-23:59`, year 0, year 10000, 9-digit fraction) -> 200 or 422, no 500.
- Path to `active` without a signed event: none found; every write to `entitlements` is in webhooks.py behind `verify`. Grace: a repeated failure does not slide the window (webhooks.py:155).
- 400 parallel requests with invented tenants (200 at once): all 402, no "too many clients", the stack answered normally afterwards.
- Secrets: `git log --all -p` for sk_/whsec_/AKIA/private keys/passwords: only the fake compose secret and the `metering` database password (loopback-bound ports, 127.0.0.1 in compose.yaml and scripts/test). No key in the working tree. The platform adapter: the key goes only in the `Authorization` header to `--base`, redirects refused, error text omits the reply.
- Dependencies in uv.lock (fastapi 0.162, starlette 1.10, pydantic 2.14, stripe 16.1, psycopg 3.3.8, urllib3 2.8, requests 2.34, certifi 2026.10, uvicorn 0.54, h11 0.16): all newer than any CVE I know of; none flagged.
- CI workflow: `pull_request` trigger only, no secrets.

## What I did not get to
- The full test suite (`scripts/test`): not run, read-only lens.
- Several uvicorn workers, multi-process webhook races (concurrency lens).
- Several concurrent 600 MB bodies (RT-security-2 only measured one).
- The platform adapter's `live` against a hostile `--base` (redirect refusal read, not run).
- Tenant ids with C1 control characters stored by hand, then logged unquoted.

# Round two (checkout origin/main 1abb7b4, fresh live stack `rt-security`, removed afterwards)
Scripts (not committed): raw.py (raw-socket body attacks), seed.py, sig.py

## 1. My round-one repros
- RT-security-1 (late same-second update revives a canceled tenant): FIXED. created=T: active, deleted -> then `updated active` same T -> `{"status":"stale"}`; `created` same T -> `stale`; edge stays 402. (Also: a same-T `subscription.created` is stale too.)
- RT-security-2 (unauthenticated body buffered): FIXED. 600 MB body, no auth: `413 0.0012s {"detail":"request body too large"}`; 100 MB chunked: 413. API memory after all attacks below: 57 MiB (idle 56).
- RT-security-3 (public placeholder webhook secret, nothing refuses it): STILL FAILS (compose.yaml unchanged; not claimed fixed).
- RT-security-4 (root container): open PR, ignored as told.
- Tenant-aliasing, signature and injection checks from round one: re-run signature set and cross-tenant set unchanged results (not repeated in full; the code for them did not change).

## 2. Attacks on the fixes
### RT-security-7 — MINOR — reconcile text output can be forged with a model name (newline + ANSI)
File: app/reconcile.py:92 (`label()`, used by every DRIFT line, including the new orphan line)
- Model names are tenant-controlled (up to 200 chars, newline and ESC allowed by /v1/usage). `label()` prints `model={model}` unquoted, so
  a drift line can start a fake line, e.g. a fake `reconcile: hours=9 rows=9 drift=0`. The real summary stays the last line, and
  `--json` escapes it, so impact is a misleading report, not a wrong exit code.
- Repro: orphan rollup row with model `m\nreconcile: hours=9 rows=9 drift=0\x1b[32m ALL CLEAN` then `python -m app.reconcile`:
  ```
  DRIFT tenant=alice model=m
  reconcile: hours=9 rows=9 drift=0^[[32m ALL CLEAN hour=2024-01-01T05:00:00Z rollup row but its hour is not in closed_hours (rollup=(1, 1, 1))
  ...
  reconcile: hours=4 rows=31 drift=1
  ```
- Fix: `label()` uses `{tenant!r}` / `{model!r}` (the webhook and the 409 text already do).

### Body cap (app/bodylimit.py): held, no bypass found
Live results (POST /v1/usage, 100 MB body unless noted, no auth):
- `/v1/usage/` -> 307 (no body read; a following client is capped on the real path); `//v1/usage`, `/V1/usage`, `/v1/usage;a=b` -> 404 (route does not match, body not read); `/v1/%75sage`, `?x=1` -> 413.
- PUT/PATCH/DELETE/OPTIONS with body -> 413; HEAD -> 405.
- `Expect: 100-continue` -> 413. Chunked without Content-Length, 100 MB -> 413 in 2 ms.
- Raw sockets: duplicate Content-Length -> 400 (h11); `Content-Length: +70000` and `0x11170` -> 400; Content-Length 5 but 70000 sent -> 422 (truncated by h11, nothing buffered); Content-Length + chunked -> 422;
  chunk size `ffffffff` -> 413; 70,000 one-byte chunks -> 413 after 0.31 s; chunked trailers of 1 MB -> 400. Memory stayed 57 MiB.
- 300 requests (100 parallel) of 32k-deep nested JSON just under the cap: all 400, `/healthz` still 200 in 2 ms. A 60 KB body with a valid event and one big extra field: 201 (extra field ignored, by design).
- 413 body leaks nothing (fixed JSON text, `connection: close`); no crash in the log.
- Only `/v1/usage` is in `limits`; the other POST route (webhook) has its own cap. NOTE (RT-security-8): any future POST route with a body is uncapped until it is added to `limits` in app/main.py.

### Charges: 409 text, `unclosed_hours`, 366-day cap: held
- 409 with 28 unpriced models (one named `evil\n2026-11-10 ERROR FAKE LINE\x1b[31mRED x`, one of 200 chars): detail is 1,088 chars, names are `repr`-escaped, "and 8 more" after 20; the log line is one physical line (`cat -v` shows no raw ESC or newline). No other tenant's model (`bob-secret-model`) appears in the 409 or in `unclosed_hours`: both queries filter by tenant.
- Range cap: exactly 366 d -> passes (409 because unpriced), +1 µs -> 422, 366 d + 1 h written with offsets `+05:00` / `-05:00` -> 422 (the diff is on instants). Year 1 and 9999 events with offsets `+23:59` / `-23:59`: 200, `unclosed_hours` correct, no 500 in the log.
### Reconcile `--json` and the orphan/not-billed queries: no injection
Both queries pass `since`/`only`/`now` as bound parameters with `::timestamptz` casts; `--json` escapes control characters (see RT-security-7 output). Not-billed sums are `numeric`, no overflow.
### New timeouts: held
Row lock held on tenant `alice` for 8 s, signed webhook sent: `503 {"detail":"database unavailable"}` after 2.0 s; after the lock was released, the same event id was `processed` (nothing recorded by the failed attempt).

## Round two result
0 blockers, 0 major, 1 minor (RT-security-7), 1 note (RT-security-8); RT-security-3 still open.
Not done in round two: root_path/proxy deployments with a real prefix (read `get_route_path`: routing and the cap use the same path, so they agree), many concurrent wide-range charges calls with millions of rows (cap exists, memory per call not re-measured).
