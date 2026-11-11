# End-to-end demo

`scripts/demo.sh` drives a running compose stack with curl: price -> entitlement -> usage -> rollup -> charges.
1. Picks the latest UTC hour (2 to 200 hours back) that is not in `closed_hours`, so it can run again on the same stack.
2. Inserts a price version for `demo-model` (1500 micros per 1k tokens in, 3000 out).
3. Sends a locally signed `customer.subscription.updated` event to `/v1/stripe/webhook` (expects 200 `processed`).
4. Shows the edge refusing an unknown tenant (402) and a request with no `X-Tenant-Id` (401).
5. Posts three usage events (201, 201, and a resend answered 200 `duplicate`).
6. Runs `python -m app.rollup` in the rollup container: it closes every due hour, this one included (the demo checks its own hour is in the output).
7. Reads the charges for the hour and asserts `total_micros == 6000`
   (3000 in and 500 out: (3000*1500 + 500*3000) / 1000).

## Run

    docker compose up -d --build
    scripts/demo.sh

Give the api a few seconds to start: `FAIL step 3: expected 200, got 000` means it was not listening yet, so run the script again.
The API port is `API_PORT` (default 8000); the api and db ports listen on 127.0.0.1 only. Expected last lines: the
charges JSON with `"total_micros": 6000`, then `demo ok`. Any unexpected status or value prints `FAIL step N:
expected X, got Y` with the body and exits non-zero.

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
If the first `docker compose up` is interrupted while the database is initialising, the half-built volume is kept and
not repaired: the demo then fails in its first steps (`relation ... does not exist`, or `FAIL step 3: expected 200,
got 500`) and the rollup container restarts in a loop. Run `docker compose down -v` (it deletes the demo data) and
start again.

## What it does not do

Test mode only, with the fake secret from `compose.yaml`. `X-Tenant-Id` is a stand-in for authentication.
Nothing is sent to Stripe.
