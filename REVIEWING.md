# Reviewing this repository

**Short version:** this is a three-day, multi-agent build of the usage-to-charge path for an AI inference platform. Models
wrote and reviewed the code; the operator designed the briefs, the merge gates and the red-team lenses. The service is the
artifact; the method is the record in [docs/how-it-was-built.md](docs/how-it-was-built.md) and
[docs/reviews/](docs/reviews/README.md). The numbers below are as of commit `4d45551`.

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
4. **Is the method real?** The roles, the gates, the red-team lenses and the mutation checks are in
   [docs/how-it-was-built.md](docs/how-it-was-built.md); the merge history is one reviewable PR per change. The README lists
   20 things that are not built, so the scope is stated rather than implied.

**What this is not.** It is a correctness core, not a production service. Identity, migrations, metrics and the ingest queue
are the next phase, and the README lists them under [Not built](README.md#not-built). It is not a Seekr integration test: no
key was available, so the adapter is built from Seekr's public API reference and tested against hand-written fixtures and a
stand-in server.

## How the agents were used

| Role | What it did | Where to see it |
|---|---|---|
| Lead | Scaffold, schema split by owner, one brief per job, merges | [docs/how-it-was-built.md](docs/how-it-was-built.md) |
| Dev A | Intake, rollups, the end-to-end scenario | [app/ingest.py](app/ingest.py), [app/rollup.py](app/rollup.py), [scripts/scenario.py](scripts/scenario.py) |
| Dev B | Prices, webhooks, entitlements, the edge | [app/pricing.py](app/pricing.py), [app/webhooks.py](app/webhooks.py), [app/edge.py](app/edge.py) |
| Review desk | Read-only review of each head; it cannot merge | [docs/reviews/](docs/reviews/README.md) |
| Red-team lenses | Money, concurrency, security, operations; a finding counts only with a repro | [docs/reviews/](docs/reviews/README.md) |
| Claims lens | README and docs checked against the code, sentence by sentence | [docs/reviews/claims.md](docs/reviews/claims.md) |

Every merge needed a review receipt, green CI and a current `main`. Before a fix merged, its guarding test was run against
the old code and seen to fail, and the new rule was broken by one line to watch the test go red. The merge history is one
reviewable PR per change.

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

## What is worth opening

| Area | Why it is worth opening |
|---|---|
| `app/seekrflow.py` | The integration seam: 94 statements built from Seekr's public API reference, tested against fixtures and a stand-in server. `live` is one command away when a key exists. |
| `app/webhooks.py` | The most rule-dense function in the service: terminal delete, same-second tie, a grace window that does not slide. |
| `app/pricing.py` | The charges pass: rates the whole range in constant memory per page, with a server-side cursor and a repeatable-read snapshot. |
| `app/rollup.py` | The hour-close protocol: a shared/exclusive advisory lock pair and a recursive CTE that walks only the hours that have events. |

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
