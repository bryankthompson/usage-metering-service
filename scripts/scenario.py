#!/usr/bin/env python3
"""Play a billing scenario against a running `docker compose up` stack, then check the API's charges independently.

Three tenants (active, past_due in grace, canceled). Each paying tenant makes inference calls through the adapter's
functions (app/seekrflow.py) against a stand-in inference server, so the usage comes from response bodies. The script
then works out the expected micro-USD itself, with its own integer arithmetic, and compares it with
GET /v1/tenants/{id}/charges. Any mismatch prints both numbers and exits 1; so does drift in `python -m app.reconcile`.
Run alone, it needs the stand-in running: `python3 scripts/stand_in_inference.py --port 8089` (scripts/up starts it).

    python3 scripts/scenario.py [--stand-in-port 8089]
    SEEKR_API_KEY=... python3 scripts/scenario.py --live      (calls the real service; never run, no key exists)

Runs on the host like scripts/demo.sh: psql, the rollup and reconcile go through `docker compose exec`.
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import seekrflow  # noqa: E402  (standard library only; the repo root must be on the path first)

SECRET = "whsec_test_only_not_a_real_secret"  # the fake secret from compose.yaml
LARGE_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
EMBED_MODEL = "BAAI/bge-large-en-v1.5"
# kind -> (model id asked of the inference server, price versions: (effective_from offset from the first hour, in, out per 1k))
# The second chat price starts 30 minutes into the first hour, not on a boundary: the hour is rated at the price in force
# at its start, so only the second hour sees the new price.
KINDS = {
    "chat-small": (seekrflow.DEFAULT_MODEL, [(None, 150, 600), (timedelta(minutes=30), 200, 800)]),
    "chat-large": (LARGE_MODEL, [(None, 900, 2700)]),
    "embed": (EMBED_MODEL, [(None, 20, 0)]),
}
# tenant -> (kind, hour offset 0 or 1, number of calls)
PLAN = {
    "active": [("chat-small", 0, 3), ("chat-small", 1, 3), ("chat-large", 1, 2), ("embed", 0, 2)],
    "past-due": [("chat-small", 0, 2), ("chat-small", 1, 2), ("chat-large", 0, 2), ("embed", 1, 2)],
}


class Fail(Exception):
    pass


# ---- the independent check: pure, no Docker, no database ----


def price_in_force(versions: list[dict], at: datetime) -> dict:
    """The version with the greatest effective_from at or before `at`; a tie goes to the later version."""
    live = [v for v in versions if v["effective_from"] <= at]
    if not live:
        raise LookupError(f"no price in force at {at.isoformat()}")
    return max(live, key=lambda v: (v["effective_from"], v["version"]))


def expected_lines(usage: dict, prices: dict) -> dict:
    """usage {(model, hour_start): (tokens_in, tokens_out)}, prices {model: [version dicts]} ->
    {(model, hour_start): {tokens_in, tokens_out, version, micros}}.
    Per rollup row: (in * input_price + out * output_price) / 1000 in integers, rounded half up once, at the price in
    force at the start of the hour."""
    lines = {}
    for (model, hour), (tokens_in, tokens_out) in usage.items():
        version = price_in_force(prices[model], hour)
        exact_thousandths = tokens_in * version["input"] + tokens_out * version["output"]
        lines[(model, hour)] = {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "version": version["version"],
            "micros": (exact_thousandths + 500) // 1000,
        }
    return lines


def expected_total(lines: dict) -> int:
    return sum(line["micros"] for line in lines.values())


# ---- the stack ----


def run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise Fail(f"{' '.join(cmd[:6])} exited {done.returncode}:\n{done.stdout}{done.stderr}")
    return done.stdout


def psql(sql: str) -> str:
    return run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "metering",
            "-d",
            "metering",
            "-tAq",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
    ).strip()


def http(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return reply.status, json.load(reply)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.load(err)
        except ValueError:
            return err.code, {}


def step(text: str) -> None:
    print(f"step: {text}", flush=True)


def expect(what: str, got, want) -> None:
    if got != want:
        raise Fail(f"{what}: expected {want!r}, got {got!r}")


def webhook(api: str, event_id: str, kind: str, obj: dict, created: int) -> None:
    payload = json.dumps(
        {"id": event_id, "object": "event", "type": kind, "created": created, "data": {"object": obj}}
    ).encode()
    now = int(time.time())
    signature = f"t={now},v1=" + hmac.new(SECRET.encode(), f"{now}.".encode() + payload, hashlib.sha256).hexdigest()
    status, body = http(
        "POST", f"{api}/v1/stripe/webhook", {"Content-Type": "application/json", "Stripe-Signature": signature}, payload
    )
    expect(f"webhook {kind} for {event_id}", (status, body.get("status")), (200, "processed"))


def open_hours() -> datetime:
    """The latest UTC hour H, starting 3+ hours ago, where H and H+1 are both not closed (H+1 ended 5+ minutes ago)."""
    text = psql("""
        SELECT to_char(h AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        FROM generate_series(date_trunc('hour', now(), 'UTC') - interval '3 hours',
                             date_trunc('hour', now(), 'UTC') - interval '200 hours', interval '-1 hour') h
        WHERE NOT EXISTS (SELECT 1 FROM closed_hours WHERE hour_start IN (h, h + interval '1 hour')) LIMIT 1""")
    if not text:
        raise Fail("no two open hours in a row in the last 200 hours")
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def closed_message(hour: datetime, during_rollup: bool, output: str = "") -> str:
    """Why a scenario hour is closed. The compose rollup worker (--every 3600) can close it before our own rollup runs."""
    if during_rollup:
        return (
            f"{hour.isoformat()} is closed, but not by this script's rollup: the rollup worker closed it first, so the "
            f"expected charges (which assume our events landed before the close) cannot hold. Re-run.\n{output}"
        )
    return f"{hour.isoformat()} is already closed: someone else (the rollup worker) closed it. Re-run to pick the next open hours."


def closed_among(hours: list[datetime]) -> list[datetime]:
    listed = ", ".join(f"'{h.isoformat()}'" for h in hours)
    found = psql(
        f"SELECT to_char(hour_start AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') FROM closed_hours WHERE hour_start IN ({listed})"
    )
    return [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in found.split()]


# ---- the scenario ----


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else text


def play(args) -> int:
    api = f"http://localhost:{os.environ.get('API_PORT') or '8000'}"
    if args.live:
        key = os.environ.get("SEEKR_API_KEY")
        if not key:
            raise Fail("--live needs SEEKR_API_KEY in the environment")
        base = seekrflow.DEFAULT_BASE
        print("LIVE: calling the real service", flush=True)
    else:
        key, base = "stand-in-key", f"http://127.0.0.1:{args.stand_in_port}/v1"
        print(f"using the stand-in inference server at {base} (not the real service)", flush=True)
    n = int(time.time())
    first = open_hours()
    hours = [first, first + timedelta(hours=1)]
    tenants = {role: f"scn-{n}-{role}" for role in ("active", "past-due", "canceled")}
    for hour in closed_among(hours):
        raise Fail(closed_message(hour, during_rollup=False))
    step(f"hours {hours[0].isoformat()} and {hours[1].isoformat()}; tenants {', '.join(tenants.values())}")

    # This run's own model labels, so a price from an earlier run is never in force for these hours.
    prices = {}
    for kind, (_, versions) in KINDS.items():
        label = f"{kind}-{n}"
        prices[label] = []
        for number, (offset, price_in, price_out) in enumerate(versions, 1):
            effective = datetime(2020, 1, 1, tzinfo=timezone.utc) if offset is None else first + offset
            psql(
                f"INSERT INTO price_versions (model, version, effective_from, input_micros_per_1k, output_micros_per_1k) "
                f"VALUES ('{label}', {number}, '{effective.isoformat()}', {price_in}, {price_out})"
            )
            prices[label].append(
                {"version": number, "effective_from": effective, "input": price_in, "output": price_out}
            )
    step("prices inserted (chat-small changes price 30 minutes into the first hour)")

    now = int(time.time())
    for role, tenant in tenants.items():
        sub = {
            "id": f"sub_{n}_{role}",
            "object": "subscription",
            "customer": f"cus_{n}_{role}",
            "status": "active",
            "metadata": {"tenant_id": tenant},
            "items": {"data": [{"price": {"id": "price_demo"}}]},
        }
        webhook(api, f"evt_{n}_{role}_1", "customer.subscription.updated", sub, now - 20)
        if role == "canceled":
            webhook(api, f"evt_{n}_{role}_2", "customer.subscription.deleted", {**sub, "status": "canceled"}, now - 10)
    webhook(api, f"evt_{n}_past-due_2", "invoice.payment_failed", {"customer": f"cus_{n}_past-due"}, now - 5)
    step("webhooks: one active, one past_due (invoice.payment_failed, in grace), one canceled")

    usage = {role: {} for role in PLAN}  # role -> {(label, hour): [tokens_in, tokens_out]}, summed from response bodies
    calls = {role: 0 for role in PLAN}
    first_event = None

    def call(role: str, kind: str, when: datetime, want: int = 201, late: bool = False) -> dict:
        model_id = KINDS[kind][0]
        if kind == "embed":
            status, response = seekrflow._post_json(
                f"{base}/inference/embeddings", {"model": model_id, "input": "demo text"}, {"Authorization": key}
            )
            expect(f"embeddings call for {role}", status, 200)
        else:
            response = seekrflow.fetch_chat(
                base, key, {"model": model_id, "messages": [{"role": "user", "content": "demo"}]}
            )
        event = seekrflow.usage_event(response, tenants[role], model_id)
        # The label carries this run's prices; the time puts the call in a past open hour. The token counts stay as received.
        event["model"], event["occurred_at"] = f"{kind}-{n}", when.isoformat()
        status, body = seekrflow.post_usage(event, api, tenants[role])
        expect(f"usage of {role} ({kind})", status, want)
        if want == 201:
            expect(f"late flag of {role} ({kind})", body.get("late"), late)
        return event

    for role, plan in PLAN.items():
        minute = 5
        for kind, offset, count in plan:
            for _ in range(count):
                event = call(role, kind, hours[offset] + timedelta(minutes=minute))
                minute += 2
                first_event = first_event or event
                row = usage[role].setdefault((event["model"], hours[offset]), [0, 0])
                row[0] += event["tokens_in"]
                row[1] += event["tokens_out"]
                calls[role] += 1
    step(f"usage: {sum(calls.values())} inference calls through the adapter, all accepted (201)")

    status, body = seekrflow.post_usage(first_event, api, first_event["tenant_id"])
    expect("resend of the first event", (status, body.get("status")), (200, "duplicate"))
    step("resend of the first event -> 200 duplicate")

    call("canceled", "chat-small", hours[0] + timedelta(minutes=50), want=402)
    status, _ = http("POST", f"{api}/v1/usage", {"Content-Type": "application/json"}, json.dumps(first_event).encode())
    expect("usage with no X-Tenant-Id", status, 401)
    step("edge: canceled tenant -> 402, no tenant header -> 401")

    out = run(["docker", "compose", "exec", "-T", "rollup", "python", "-m", "app.rollup"])
    print(out, end="")
    for hour in hours:
        if f"closed hour={hour.isoformat()} rows=" not in out:
            if hour in closed_among([hour]):
                raise Fail(closed_message(hour, during_rollup=True, output=out))
            raise Fail(f"the rollup did not close {hour.isoformat()}:\n{out}")
    step("rollup closed both hours")

    call(
        "active", "chat-small", hours[0] + timedelta(minutes=40), late=True
    )  # its hour is closed: stored, never billed
    step("late event (its hour is closed) -> 201 late=true, left out of the expected charge")

    table, failed = [], False
    start, end = hours[0].strftime("%Y-%m-%dT%H:%M:%SZ"), (hours[1] + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for role in PLAN:
        tenant = tenants[role]
        status, charges = http(
            "GET", f"{api}/v1/tenants/{tenant}/charges?start={start}&end={end}", {"X-Tenant-Id": tenant}
        )
        expect(f"charges of {tenant}", status, 200)
        got = {
            (line["model"], datetime.fromisoformat(line["hour_start"])): {
                "tokens_in": line["tokens_in"],
                "tokens_out": line["tokens_out"],
                "version": line["price_version"],
                "micros": line["micros"],
            }
            for line in charges["lines"]
        }
        want = expected_lines({k: tuple(v) for k, v in usage[role].items()}, prices)
        total = expected_total(want)
        # The client rule (docs/billing.md, Paging): the lines held must be the whole range and add up to the total.
        ok = (
            got == want
            and charges["total_micros"] == total
            and charges["next_cursor"] is None
            and charges["line_count"] == len(charges["lines"]) == len(want)
            and sum(line["micros"] for line in charges["lines"]) == charges["total_micros"]
        )
        if not ok:
            failed = True
            print(f"MISMATCH {tenant}: API total {charges['total_micros']} vs expected {total}", file=sys.stderr)
            for k in sorted(set(got) | set(want)):
                if got.get(k) != want.get(k):
                    print(f"  {k[0]} {k[1].isoformat()}: API {got.get(k)} vs expected {want.get(k)}", file=sys.stderr)
        status_text = psql(f"SELECT status FROM entitlements WHERE tenant_id = '{tenant}'")
        table.append(
            (
                tenant,
                status_text,
                str(calls[role]),
                str(sum(v[0] for v in usage[role].values())),
                str(sum(v[1] for v in usage[role].values())),
                str(charges["total_micros"]),
                str(total),
                ok,
            )
        )
    table.append(
        (
            tenants["canceled"],
            psql(f"SELECT status FROM entitlements WHERE tenant_id = '{tenants['canceled']}'"),
            "0",
            "-",
            "-",
            "refused 402",
            "-",
            True,
        )
    )

    head = ("tenant", "status", "calls", "tokens in", "tokens out", "charge", "expected", "")
    widths = [max(len(str(r[i])) for r in [head] + table) for i in range(7)]
    print("\n" + "  ".join(h.ljust(w) for h, w in zip(head, widths + [0])))
    for row in table:
        verdict = paint("OK", "32") if row[7] else paint("FAIL", "31")
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)) + "  " + verdict)
    print("charge = micro-USD from the API; expected = the script's own arithmetic on the token counts it received\n")

    rec = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-m", "app.reconcile", "--since", hours[0].isoformat()],
        capture_output=True,
        text=True,
    )
    print(rec.stdout.strip().splitlines()[-1] if rec.stdout.strip() else "reconcile printed nothing")
    if rec.returncode != 0:
        failed = True
        print(f"reconcile exited {rec.returncode}:\n{rec.stdout}{rec.stderr}", file=sys.stderr)
    if failed:
        raise Fail("the API and the independent check disagree (or reconcile found drift)")
    print("scenario ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="billing scenario with an independent check of the charges")
    parser.add_argument("--stand-in-port", type=int, default=8089)
    parser.add_argument(
        "--live", action="store_true", help="call the real service instead of the stand-in (needs SEEKR_API_KEY)"
    )
    try:
        return play(parser.parse_args(argv))
    except Fail as err:
        print(f"FAIL: {err}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, seekrflow.MissingUsage, urllib.error.URLError) as err:
        print(f"FAIL: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
