# Review reports

Five reviews of this code, each kept here as the reviewer wrote it, so the counts in the [README](../../README.md#how-it-was-built)
and in [how it was built](../how-it-was-built.md) can be checked against the source. Read them as a record of what was found
and what was not reached, not as a certificate: every reviewer was a model, and each report lists what it did not get to.

**Method.** Each review was a read-only lane: it changed no product code and its attack scripts were not committed. A finding
counted only with a command or a failing test that shows it, so every finding carries its repro, and each report also lists what
was tried and held. Later rounds re-ran the earlier repros against the fixed code and then attacked the fixes.

| Lens and round | Report | SHA tested | Blocker | Major | Minor | Scope |
|--|--|--|--|--|--|--|
| Money, round 1 | [money.md](money.md) | `da520b6` | 0 | 3 | 3 (+1 note) | Rating, price versions, unpriced models, late and future events, reconcile, charges completeness |
| Money, round 2 | [money.md](money.md) | origin/main, "47 commits after da520b6" (SHA not named in the report) | 0 | 0 | 2 (+1 note) | Re-run of round one; attacks on `unclosed_hours` and on charge windows that are not on an hour boundary |
| Concurrency, round 1 | [concurrency.md](concurrency.md) | `da520b6` | 0 | 2 | 1 | Same-second cancel, a held hour lock hanging the API, the grace window under parallel delivery, ingest racing the close |
| Concurrency, round 2 | [concurrency.md](concurrency.md) | `1abb7b4` | 0 | 1 | 2 | Re-run; attacks on the lock timeouts and per-hour isolation (the major is an older open case, RT-concurrency-4) |
| Concurrency, round 3 | [concurrency.md](concurrency.md) | `a89fa21` | 0 | 0 | 2 | The terminal-delete rule for a cancel |
| Security, round 1 | [security.md](security.md) | `da520b6` | 0 | 2 | 2 (+2 notes) | Signatures, tenant isolation, request body size, injection, secrets, dependencies |
| Security, round 2 | [security.md](security.md) | `1abb7b4` | 0 | 0 | 1 (+1 note) | Re-run; attacks on the body cap, timeouts and the 409 text |
| Operations, round 1 | [operations.md](operations.md) | `da520b6` | 0 | 2 | 4 (+2 notes) | A paused database, a poisoned rollup hour, volumes, charges size, outage and flood runs |
| Operations, round 2 | [operations.md](operations.md) | `1abb7b4` | not stated | 2 | 2 (+1 note) | Re-run; the runbook run literally; counted from the headings, the report gives no total |
| Claims, first pass | [claims.md](claims.md) | `843163a` (full SHA in the report) | not stated | 0 | 9 (+6 notes) | README and docs checked sentence by sentence against the code, from a clean clone |
| Claims, re-check | [claims.md](claims.md) | `82bc2df` | not stated | 0 | 0 (+1 note) | The runbook, testing and demo pages, and one paragraph of the ingest page |
| Claims, final check (before #38) | [claims.md](claims.md) | `e603a15` | not stated | 0 | 1 (+2 notes) | The diff since `82bc2df`; name and secret sweeps of the tree and the history |

The first four lenses add up to the totals the README gives: round 1, 0 blockers, 9 major and 10 minor; round 2, 0 blockers reported (the
operations report gives no count), 3 major and 7 minor. The claims lens did not count blockers.

**What was changed from the originals.** A header line was added to each report. Local checkout paths were shortened to the
file or script name (the attack scripts named in the reports, such as `a1.py` or `wh.py`, are not in the repository). The
platform adapter's name and its file names were replaced by "the platform adapter" and "the adapter's ..." because the
repository keeps that name in the adapter's own files only, and one email address was replaced by words. Findings, counts and
wording are otherwise as written. Line numbers in a report point at the commit it tested, not at today's tree.
