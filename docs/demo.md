# End-to-end demo

`scripts/demo.sh` drives a running compose stack with curl and proves the whole path:
price -> entitlement -> usage -> rollup -> charges.
1. Picks the latest UTC hour (2 to 200 hours back) that is not in `closed_hours`, so it can run again on the same stack.
2. Inserts a price version for `demo-model` (1500 micros per 1k tokens in, 3000 out).
3. Sends a locally signed `customer.subscription.updated` event to `/v1/stripe/webhook` (expects 200 `processed`).
4. Shows the edge refusing an unknown tenant (402) and a request with no `X-Tenant-Id` (401).
5. Posts three usage events (201, 201, and a resend answered 200 `duplicate`).
6. Runs `python -m app.rollup` in the rollup container: it closes every due hour, this one included (`rows=1 events=2`).
7. Reads the charges for the hour and asserts `total_micros == 6000`
   (3000 in and 500 out: (3000*1500 + 500*3000) / 1000).

## Run

    docker compose up -d --build
    scripts/demo.sh

The API port is `API_PORT` (default 8000). Expected last lines: the charges JSON with `"total_micros": 6000`, then `demo ok`.
Any unexpected status or value prints `FAIL step N: expected X, got Y` with the body and exits non-zero.

## The database
The schema is created by mounting `./db` into the db container's `/docker-entrypoint-initdb.d`. Postgres runs
those files only when the data volume is first created: an existing volume is NOT re-initialised.
`docker compose down -v` deletes the volume and resets it.

## What it does not do

Test mode only, with the fake secret from `compose.yaml`. `X-Tenant-Id` is a stand-in for authentication.
Nothing is sent to Stripe.
