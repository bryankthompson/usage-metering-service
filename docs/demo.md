# End-to-end demo

`scripts/demo.sh` drives a running compose stack with curl: price -> entitlement -> usage -> rollup -> charges.
1. Picks the latest UTC hour (2 to 200 hours back) that is not in `closed_hours`, so it can run again on the same stack.
2. Inserts a price version for `demo-model` (1500 micros per 1k tokens in, 3000 out).
3. Sends a locally signed `customer.subscription.updated` event to `/v1/stripe/webhook` (expects 200 `processed`).
4. Shows the edge refusing an unknown tenant (402) and a request with no `X-Tenant-Id` (401).
5. Posts three usage events (201, 201, and a resend answered 200 `duplicate`).
6. Runs `python -m app.rollup` in the rollup container: it closes every due hour, this one included (the demo checks its own hour is in the output).
7. Reads the charges for the hour and asserts `total_micros == 6000`
   (3000 in and 500 out: (3000*1500 + 500*3000) / 1000), no `next_cursor`, and lines that add up to the total (the client rule
   of [billing.md](billing.md), Paging).

## Run
Needs on the host: docker compose v2 (`docker compose`; `up --wait` for the restore recipe below), python3 and curl (the script signs the webhook with python3).
Free ports: 8000 and 5432, or set `API_PORT` and `DB_PORT`.

    docker compose up -d --build --wait
    scripts/demo.sh

Use `--wait` (or `scripts/up`) so the command returns when the api answers; a plain `up -d` does not. `FAIL step 3: expected 200, got 000` means it was not listening yet, so run the script again.
The API port is `API_PORT` (default 8000); the api and db ports listen on 127.0.0.1 only. Expected last lines: the
charges JSON with `"total_micros": 6000`, then `demo ok`. Any unexpected status or value prints `FAIL step N:
expected X, got Y` with the body and exits non-zero.
Two things in the output are not faults. Step 4 makes the api log two WARNING lines on purpose, the refusals it checks:
`edge denied: tenant='nobody-...' status=402 reason=no entitlement` and `edge denied: tenant=None status=401 reason=X-Tenant-Id header
is required`. And step 6 prints the rollup's own `closed hour=...` line right before the script's own `step 6: rollup -> closed hour=...`
line, so the hour appears twice.

CI runs this script on every pull request and push to `main`, as the smoke steps at the end of the `test` job: `docker compose up -d --build --wait`,
`scripts/demo.sh`, then `docker compose down -v`. There the db publishes port 5433, because the job's own postgres service holds 5432.

## One command: `scripts/up`

`scripts/up` goes from a cold checkout to a priced, checked result. It runs `docker compose up -d --build --wait` (the api has a
healthcheck on `/healthz`, so `--wait` returns when it answers), polls `/healthz` for up to 30 seconds and fails loudly if it
does not answer, starts `scripts/stand_in_inference.py` in the background (stopped when the script ends), runs
`scripts/scenario.py`, and prints the seconds each stage took and the total. The times are measured on each run; none is promised.

The scenario plays three tenants (active, past_due in grace after `invoice.payment_failed`, canceled). The two paying tenants make
inference calls through the `seekrflow` adapter's functions (`app/seekrflow.py`), so tokens in and out come from response bodies: chat with
two models, and embeddings. It also sends a resend (200 duplicate), a late event (its hour is closed: 201 `late`, not billed) and a
price change 30 minutes into the first hour (the hour is rated at the price in force at its start, so only the second hour sees
it). The edge refuses the canceled tenant (402) and a request with no tenant header (401). It runs the rollup and reads
`/v1/tenants/{id}/charges`.

The independent check: the script works out the expected micro-USD from the token counts it received, with its own integer
arithmetic (rounded half up once per rollup row), and compares each charge line and total with the API. A mismatch prints both
numbers and exits 1. It then runs `python -m app.reconcile` and fails on drift. The output ends with a table of tenant, status,
calls, tokens, charge, expected and OK/FAIL. The script labels each call's model with a per-run suffix (so an earlier run's
prices are never in force) and sets `occurred_at` itself (the call is made now, the usage lands in a past open hour); the token
counts are untouched. Each run uses fresh tenant ids and two unclosed hours, so it can run again on the same stack.

The stand-in inference server is not the real service: it answers the chat and embeddings routes with made-up text and token
counts that vary per request but repeat for the same `--seed`. It prints `STAND-IN SERVER: not the real service` on start.
Everything the scenario shows about usage therefore proves the metering and billing path, not any real model's behaviour.

`python3 scripts/scenario.py --live` skips the stand-in, needs `SEEKR_API_KEY` (it fails if missing and never prints it), prints
`LIVE: calling the real service` and calls the platform's default base through the `seekrflow` adapter. **That path has never run**: there
is no key. Treat it as untested.

Ports: `API_PORT` and `DB_PORT` as above, `STAND_IN_PORT` (default 8089) for the stand-in. The pure parts (the expected-charge
arithmetic and the stand-in) are tested without Docker in `tests/test_scenario.py`.

## The database
The data lives in a named volume, `pgdata` (`docker volume ls` shows it as `<project>_pgdata`; the project is the directory
name or `COMPOSE_PROJECT_NAME`), mounted at `/var/lib/postgresql/data`. `docker compose down` and a recreated container keep it, so
prices, usage and entitlements survive; `docker compose down -v` deletes it and resets the database.
The schema is created by mounting `./db` into the db container's `/docker-entrypoint-initdb.d`. Postgres runs
those files only when the data volume is first created: an existing volume is NOT re-initialised.
A stack started before the named volume existed kept its data in an anonymous volume that the compose file no longer mounts: that
volume stays on disk (`docker volume ls`) and the new database starts empty. To keep the data, on the old stack run
`docker compose exec -T db pg_dump -U metering --data-only metering > data.sql` and `docker compose down`; switch to the new
file; then `docker compose up -d --wait db` (waits for the healthcheck), `docker compose exec -T db psql -v ON_ERROR_STOP=1 -U metering -d metering < data.sql` and `docker compose up -d`.
If the first `docker compose up` is interrupted while the database is initialising, the half-built volume is kept: the
demo then fails in its first steps (`relation ... does not exist`, `database "metering" does not exist`, or `FAIL step 3:
expected 200, got 500`) and the rollup container restarts in a loop (`no pg_hba.conf entry ... database "metering"`).
`up --wait` can still return 0 on that stack, because no healthcheck opens the `metering` database: the db's is
`pg_isready`, the api's is `/healthz` (the process only) and the rollup has none. Check with
`docker compose exec -T db psql -U metering -d metering -c '\dt'`, which lists the tables on a good stack. The way back is
`docker compose down -v`, then `docker compose up -d --build --wait` again: a first start holds no data to keep, and
`down -v` deletes the volume. Seven kills of the db container at different points of the first start, each followed by
that fix, all ended at `demo ok`.
To add a column that a newer `db/` file brings to a stack that has data, apply the schema to the running stack, which keeps
the data: `docker compose exec -T api python -c "from app import db; c=db.connect(); db.apply_schema(c); print('applied')"`
(run twice on a demo stack with data it printed `applied` both times and kept the rows; with a column dropped by hand it
brought the column back).

## What it does not do

The compose rollup worker runs hourly: if it closes the scenario's hours mid-run, the scenario fails loudly (the expected charges assume its own events landed before the close) and a re-run picks fresh hours.

Test mode only, with the fake secret from `compose.yaml`. `X-Tenant-Id` is a stand-in for authentication.
Nothing is sent to Stripe.
