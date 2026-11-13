# Reviewing this repository

**Short version:** this is a multi-agent build of the usage-to-charge path for an AI inference platform. Models
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
3. **Is the evidence trustworthy?** The suite runs against a real PostgreSQL (counts in the table below); about 98% line
   coverage; ruff and mypy clean; a coverage floor of 92% in CI; a Docker build, a compose config check and an end-to-end smoke in the same CI job.
4. **Is the method real?** The roles, the gates, the red-team lenses and the mutation checks are in
   [docs/how-it-was-built.md](docs/how-it-was-built.md); the merge history is one reviewable PR per product change, with four
   scaffold and setup commits landing directly on `main` before the gate was in place. The README states
   the scope item by item under Not built.

**Scope.** This is a correctness core. Identity and the ingest queue are under [Not built](README.md#not-built); migrations,
metrics and replicas are in the table below. The adapter is built from the platform's public documentation and tested against
hand-written fixtures and a stand-in server; `seekrflow live` is the command that takes a real key.

## How the agents were used

| Role | What it did | Where to see it |
|---|---|---|
| Lead | Scaffold, schema split by owner, one brief per job, merges | [docs/how-it-was-built.md](docs/how-it-was-built.md) |
| Dev A | Intake, rollups, the end-to-end scenario | [app/ingest.py](app/ingest.py), [app/rollup.py](app/rollup.py), [scripts/scenario.py](scripts/scenario.py) |
| Dev B | Prices, webhooks, entitlements, the edge | [app/pricing.py](app/pricing.py), [app/webhooks.py](app/webhooks.py), [app/edge.py](app/edge.py) |
| Review desk | Read-only review of each head; it cannot merge | [docs/reviews/](docs/reviews/README.md) |
| Red-team lenses | Money, concurrency, security, operations; a finding counts only with a repro | [docs/reviews/](docs/reviews/README.md) |
| Claims lens | README and docs checked against the code, sentence by sentence | [docs/reviews/claims.md](docs/reviews/claims.md) |

Every merge needed green CI, a current `main`, and a review receipt or the merging lead's written reason (69 receipts and 14
written reasons through #84; the receipts are kept outside the repo, which keeps each merge commit and the build log). Each
lane was briefed to run a fix's guarding test against the old code and see it fail, then break the new rule by one line and
watch the test go red; [docs/how-it-was-built.md](docs/how-it-was-built.md) lists the exceptions. The merge history is one
reviewable PR per product change, with four scaffold and setup commits landing directly on `main` before the gate was in place.

## The numbers

| Measure | Value |
|---|---|
| Working time | About 22 hours of commit activity across 2026-11-10 to 2026-11-12 (windows with gaps under 90 minutes); sessions ran largely unattended, with the lead, the advisor and the operator steering at decision points; per-day windows are in [docs/build-story.md](docs/build-story.md) |
| Commits / merged PRs | 412 / 85 |
| Application code | 2,569 lines in 13 modules |
| Tests | 8,399 lines in 36 files; 849 passing |
| Coverage | about 98% line coverage (CI floor 92%) |
| Static checks | ruff format, ruff check, mypy — clean |
| Review | 5 lenses, 2 red-team rounds plus follow-up passes: 0 blockers, 12 major, 17 minor, plus 10 minor from the claims lens (9 first pass, 1 final check) |

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
| A call to the real platform service | The adapter runs on hand-written fixtures and a stand-in server; no key was available | Validate `app/seekrflow.py` against one real response, then contract tests |
| Kafka or a queue | The scope is the correctness core, not the ingest buffer | A topic partitioned by tenant, a batch consumer, an outbox |
| Kubernetes | Compose is the demo target | Manifests, HPA, PDB, readiness |
| Versioned migrations | `apply_schema` is idempotent DDL in filename order, not a migration tool | Alembic or Sqitch, expand/contract |
| Metrics and tracing | Logs only | OpenTelemetry, RED/USE, rollup lag |
| HA or multi-region | One Postgres | Replicas, failover, partitioning |

The README's [Not built](README.md#not-built) section lists the rest.

## What is worth opening

| Area | Why it is worth opening |
|---|---|
| `app/seekrflow.py` | The integration seam: built from the platform's public documentation, tested against hand-written fixtures and a stand-in server. `seekrflow live` is the command that takes a real key. |
| `app/webhooks.py` | The most rule-dense function in the service: terminal delete, same-second tie, a grace window that does not slide. |
| `app/pricing.py` | The charges pass: rates the whole range in constant memory per page, with a server-side cursor and a repeatable-read snapshot. |
| `app/rollup.py` | The hour-close protocol: a shared/exclusive advisory lock pair and a recursive CTE that walks only the hours that have events. |

## Run it

```bash
docker compose up -d --build --wait   # Postgres 16, the API on 127.0.0.1:8000, the hourly rollup
scripts/demo.sh                       # ends with "demo ok"
scripts/test                          # the full suite, about 4 minutes
```

If Docker is unavailable, the CI run is the reference: `.github/workflows/ci.yml` brings up the same stack and runs the same
demo on every pull request and on `main`.

## If you have 30 seconds

Open [docs/reviews/README.md](docs/reviews/README.md). It lists what five independent review lenses found, with a command or
a failing test for each finding, and what they did not reach. Then open [app/rollup.py](app/rollup.py#L1-L6). That is the
thesis: a hard invariant, stated first, enforced in the database, and attacked by a test that would fail if it broke.
