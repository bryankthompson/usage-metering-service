# Reviewing this repository

**Short version:** this is a three-day, multi-agent build of the usage-to-charge path for an AI inference platform. It is a
correctness core and a record of how it was checked — not a production service. The full build record is in
[docs/how-it-was-built.md](docs/how-it-was-built.md); the review reports are in [docs/reviews/](docs/reviews/README.md).
The numbers below are as of commit `4d45551`.

## What to judge

1. **Are the invariants right?** The central one: every event of a closed hour is in the closed rollup or flagged `late`,
   never both and never neither ([app/rollup.py](app/rollup.py#L1-L6)). Money is integer micro-USD with one rounding
   ([app/pricing.py](app/pricing.py#L107-L111)). A missing price refuses the bill
   ([app/pricing.py](app/pricing.py#L35-L36)).
2. **Is the hard part handled?** The ingest-versus-close race is serialized with a shared/exclusive advisory lock pair
   ([app/rollup.py](app/rollup.py#L74-L76), [`:102`](app/rollup.py#L102)) and attacked by a 12-round, 6-thread test
   ([tests/test_attack_ingest.py](tests/test_attack_ingest.py#L216-L271)). Out-of-order Stripe events are handled with a
   terminal-delete rule and a same-second tie rule ([app/webhooks.py](app/webhooks.py#L140-L162)).
3. **Is the evidence trustworthy?** 848 tests pass against a real PostgreSQL, 1 skipped; 98% line coverage; ruff and mypy
   clean; a coverage floor of 92% in CI; a Docker build, a compose config check and an end-to-end smoke in the same CI job.
4. **Is the scope honest?** The README lists 20 things that are not built. The review reports list what each reviewer did
   not reach. Nothing claims to be production-ready.

**What this is not.** It is not a production service: identity is a header, and there are no migrations, metrics or queue.
It is not a Seekr integration test: no key was available, so the adapter is built from the published schema and tested
against hand-written fixtures. It is not unattended: every merge needed a review receipt and green CI, and the operator read
the five functions on the money and entitlement path.

## The numbers

| Measure | Value |
|---|---|
| Elapsed | 3 days (2026-11-10 to 2026-11-12) |
| Commits / merged PRs | 412 / 85 |
| Application code | 2,569 lines in 13 modules |
| Tests | 8,399 lines in 36 files; 848 passing, 1 skipped |
| Coverage | 98% line coverage (CI floor 92%) |
| Static checks | ruff format, ruff check, mypy — clean |
| Review | 5 lenses, 2 rounds: 0 blockers, 12 major, 17 minor, plus 9 minor from the claims lens |

## The 10-minute path

1. [app/rollup.py](app/rollup.py#L1-L6), [`:74-76`](app/rollup.py#L74-L76), [`:102`](app/rollup.py#L102) — the invariant
   and the lock protocol.
2. [tests/test_attack_ingest.py](tests/test_attack_ingest.py#L216-L271) — the race that proves it.
3. [app/pricing.py](app/pricing.py#L107-L111) — the money rule, in one line of integer arithmetic.
4. [app/webhooks.py](app/webhooks.py#L140-L162) — the cancel and same-second tie rules.
5. [docs/reviews/README.md](docs/reviews/README.md) — what five independent review lenses found, with repros, and what
   they missed.

## Out of scope, and why

| Not built | Why it is not here | What production needs |
|---|---|---|
| Real authentication | The tenant is an `X-Tenant-Id` header; the stack binds to 127.0.0.1 and is test-mode only | OIDC/JWT, a tenant claim, per-tenant rate limits |
| A live Seekr call | The API is demo-request only; no key was available | Validate `app/seekrflow.py` against one real response, then contract tests |
| Kafka or a queue | The scope is the correctness core, not the ingest buffer | A topic partitioned by tenant, a batch consumer, an outbox |
| Kubernetes | Compose is the demo target | Manifests, HPA, PDB, readiness |
| Versioned migrations | `apply_schema` is idempotent DDL in filename order, not a migration tool | Alembic or Sqitch, expand/contract |
| Metrics and tracing | Logs only | OpenTelemetry, RED/USE, rollup lag |
| HA or multi-region | One Postgres | Replicas, failover, partitioning |

The full list is the README's [Not built](README.md#not-built) section.

## Where the risk is concentrated

| Area | Why it matters | What to inspect |
|---|---|---|
| `app/seekrflow.py` | The only unverified seam: no key, so the real response shape is unseen | 94 statements, hand-written fixtures, a stand-in server; `live` is the one command that needs a key |
| `app/bodylimit.py` | FastAPI parses the body before dependencies, so the cap must run before the edge | The ASGI guard, `Content-Length`, and the stream counter |
| `app/webhooks.py` | Highest complexity (E/38) and the subtlest state machine | Terminal delete, same-second tie, grace window that does not slide |
| `app/pricing.py` | Second highest (D/23); it rates the whole range on every page | Server-side cursor, repeatable-read snapshot, cursor validation |

## Run it

```bash
docker compose up -d --build --wait   # Postgres 16, the API on 127.0.0.1:8000, the hourly rollup
scripts/demo.sh                       # ends with "demo ok"
scripts/test                          # 848 tests, about 4 minutes
```

If Docker is unavailable, the CI run is the reference: `.github/workflows/ci.yml` brings up the same stack and runs the same
demo on every pull request and on `main`.

## If you have 30 seconds

Open [docs/reviews/README.md](docs/reviews/README.md). It lists what five independent review lenses found, with a command or
a failing test for each finding, and what they did not reach. Then open [app/rollup.py](app/rollup.py#L1-L6). That is the
thesis: a hard invariant, stated first, enforced in the database, and attacked by a test that would fail if it broke.
