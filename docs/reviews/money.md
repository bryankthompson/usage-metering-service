# Red team, lens: money (checkout of origin/main da520b6)

> Review report as written at the time. File:line citations refer to the SHA named in the report, not to today's tree.

> Note, 2026-11-11: in this report "the platform adapter" is seekrflow (app/seekrflow.py); no live call to the real service was ever made.

Attack scripts: a1.py to a4.py (uncommitted, own database rt_money in the shared test container).
Counts: 0 BLOCKER, 3 MAJOR, 3 MINOR, 1 NOTE. Test suite not re-run (read-only lane; no product change).

## RT-money-1  MAJOR  A price change inside an hour does not apply from its effective time
app/pricing.py:117-136 (charge_lines rates each rollup row at its `hour_start`), docs/billing.md:"a price change mid-period applies from its effective time".
Rollups are per hour, and the whole hour is rated at the price in force at hour_start. A version effective 10:30 does not touch usage at 10:45; it starts at 11:00.
Usage in [effective_from, next hour) is billed at the OLD price. The docs promise the effective time.
Repro (attack script a2.py): price v1 1000/2000 from 2026-11-01; v2 5000/5000 effective 10:30; 1000 input tokens at 10:45 and at 11:45.
Observed: `200 [('2026-11-01T10:00:00Z', 1, 1000), ('2026-11-01T11:00:00Z', 2, 5000)]` (the 10:45 event is rated with v1).
Smallest fix: document that a price takes effect at the first hour boundary at or after effective_from, and reject (or round up) an effective_from that is not on an hour boundary in add_price_version.

## RT-money-2  MAJOR  A back-dated price version silently rewrites bills already computed
app/pricing.py:44-62 (add_price_version has no floor), 82-90 and 117-136 (selection by greatest effective_from <= hour).
docs/billing.md says a stored version is never changed "so a past charge can be rebuilt". The rows are immutable, but nothing stops a NEW version whose effective_from lies in the past,
and it wins over older versions for every hour after it, so the same period gives a different total than before. A tie on effective_from also silently goes to the later version.
Repro (attack script a3.py): charges for 2026-11-01T00:00Z to 12:00Z returned total 6000. Then add_price_version(conn, "m", 2026-11-01T10:00Z, 9000, 9000).
Observed: `before 6000 after 14000 [(4, 9000), (2, 5000)]` (hour 10 was rated with v1 before; the same query now uses v4).
Smallest fix: add_price_version refuses effective_from earlier than the newest existing effective_from for that model (or earlier than the newest closed hour), and refuses an equal one.

## RT-money-3  MAJOR  One unpriced model makes the tenant's whole charges request fail with 409
app/pricing.py:136-140 (NoPrice raised for the first row without a price), app/ingest.py (model is any 1 to 200 character text, nothing normalised or checked against price_versions).
The tenant can send (or a typo / different case / trailing space can produce) a model with no price. Its rollup row makes every charges query that covers the hour 409, including the hours of correctly priced models.
No line is returned for the good rows, so the bill for the period cannot be produced until a price is added, and nothing at ingest warns.
Model names are compared byte for byte: `GPT`, `gpt` and `GPT ` are three models (also the same for Unicode lookalikes); each needs its own price or it blocks the bill.
Repro (attack script a2.py): prices exist for `m` and `GPT`; events for `gpt`, `GPT`, `GPT ` accepted 201 in the same hour; after the hour closes:
`(409, {'detail': "no price for model 'gpt' at 2026-11-01T12:00:00+00:00"})` for a range that also holds priced usage.
Smallest fix: at ingest, normalise the model (strip, casefold/NFKC) and either reject a model with no price (422) or accept it; and in charges return the priced lines plus a list of unpriced models instead of failing the whole bill.

## RT-money-4  MINOR  Usage can be dodged by back-dating `occurred_at`; late and future events are invisible everywhere
app/ingest.py:46-54 (late = hour already in closed_hours), app/rollup.py:33 (NOT late), app/pricing.py (charges read rollups only), app/reconcile.py (excludes late, docs/reconcile.md says it does not count them).
`occurred_at` is client-supplied. An event whose hour is already closed is stored with late=true and is never billed; a tenant (or a client with a spooled/retried response, see the platform adapter's `--spool`) can choose that hour. An event dated
9999-12-31 is accepted and never closed, so also never billed. Nothing reports how many late events or how many tokens exist: ingest answers 201 and the totals and reconcile are both clean. docs list late events as "not billed", but not that the client can pick it.
Repro (attack script a2.py, a4.py): `POST /v1/usage` model GPT, 1e9 tokens, occurred_at 2026-11-01T10:00:00Z (hour closed) -> `201 {'status': 'accepted', 'event_id': 6, 'late': True}`;
`select count(*), sum(tokens_in) from usage_events where late` -> `1 / 1000000000`, charges unchanged. Same with 9999-12-31T23:30:00Z: `201 ... 'late': False`, never closed.
Severity is MINOR only because the tenant header is already spoofable and the docs call late unbilled; it is MAJOR if tenants are not trusted to date their own usage.
Smallest fix: ingest rejects occurred_at more than a small skew ahead of received_at; add a late-events count/token sum to reconcile output (exit non-zero or at least print it) and to charges.

## RT-money-5  MINOR  Reconcile says clean when a closed hour has lost its closed_hours row
app/reconcile.py:21 (CLOSED_HOURS), :84-86 (both RECOMPUTE and STORED are filtered by that hour list).
A rollup row whose hour is not in closed_hours is not read at all, while charges still bills it. docs say it checks "every closed hour", so this is within the letter, but "clean" does not cover a rollup the bill uses.
Repro (attack script a3.py): inflate a closed hour's tokens_in x100, then `DELETE FROM closed_hours` for it: `reconcile: hours=2 rows=4 drift=0` exit 0, and charges total 905000 (inflated).
Needs database write access. Smallest fix: STORED should be read for all hours (or at least report rollup hours not in closed_hours as drift).

## RT-money-6  MINOR  Doc/code: charges silently omit open hours
app/pricing.py:117 reads usage_rollups_hourly only. Usage in the current or any unclosed hour (rollup waits grace 5 min after the hour, runs every 3600 s so up to about 65 min) is absent from `total_micros` with no flag in the response.
A caller asking for "today" gets a total that is lower than real usage. Docs say it rates rollup rows, but not that the response is partial.
Smallest fix: return the latest closed hour (or the count of unclosed hours with events) in the charges response.

## RT-money-7  NOTE  total_micros can exceed 2^63
Prices are BIGINT up to 2^63-1 and tokens up to 1e12 per event: one line is 1.8446744073709551614e28/1000, exact in Python and in JSON, but any int64 client reading `total_micros` overflows. attack script a1.py: `_rate(big, 10**12, 10**12)` = 18446744073709551614000000000. Fix only if a price ceiling is wanted (CHECK on the price columns).

## What I tried that held
- Rounding: 1 token at 1500 -> 2; 499/500 tokens at 1 -> 0/1; 0 tokens -> 0; per-line rounding sums to less than the aggregate (1000 lines of 1 token at 400 = 0 vs 400): by design and documented.
- Integer overflow: all rating is Python int; no float in the money path.
- Timestamps: `23:59:60Z` and `24:00:00Z` are 422; `0001-01-01T00:00:00Z` and `23:59:59.999999Z` accepted into the right hour; a 7-digit fraction is truncated to 23:59:59.999999 (same hour).
- Charges edges: start == end 422; a year 1 to 9999 range in any offset answers (409 only because of RT-money-3, not a crash); a 1-microsecond range 200 with no lines.
- Duplicate key: same payload 200, different payload 409 (existing tests, and by reading the ON CONFLICT path).
- Price tie and DST: tie goes to the later version; comparison in UTC (_in_force).
- The platform adapter: reading the mapping (created -> UTC ISO, completion required unless data-only response, bools refused); `id` missing gives `error: 'id'` (KeyError), loud but unclear, not reported as a finding.

## What I did not get to
- The ingest/close race and two rollup processes (that is the concurrency lens).
- PGTZ non-UTC runs of my scripts (the docs state the 0001-01-01 limit).
- The full pytest suite and a live docker compose stack; I used the app in-process (TestClient) on the real Postgres 16.
- Reconcile `--actuals` tolerance math beyond reading it (|r-a|*10000 > bps*a is integer; actual=0 with rollup>0 is drift, as expected).
- The platform adapter's live path (no key, no network by rule).

# Round two (origin/main, 47 commits after da520b6)

Scripts: b1.py and b2.py (uncommitted; own database rt_money2, dropped after).
Counts: 0 blockers, 0 major, 2 minor, 1 note. The suite was not re-run.

## 1. Re-run of round-one repros
- RT-money-1 (mid-hour price): CHANGED, docs only. docs/billing.md:13-14 now says a version effective 10:30 first applies to the 11:00 hour. Code is the same: `200 [(1, 1000)]`, the 10:45 event is still rated with v1. Accepted as documented.
- RT-money-2 (back-dated version): CHANGED, docs only (docs/billing.md:16 and :29). add_price_version still has no floor: a version at 2026-11-01T10:00 took the same range from 1000 to 9000 (`200 9000`). Accepted as documented, but nothing in code stops it.
- RT-money-3 (unpriced model): CHANGED. Still all-or-nothing 409, but the message now names every model: `409 no price for 2 models: 'GPT ' at 2026-11-01T12:00:00+00:00, 'gpt' at 2026-11-01T12:00:00+00:00`. Model names are still not normalised (`GPT ` and `gpt` stay separate models), so the cause is still open; the operator can now fix it in one pass.
- RT-money-4 (back-dating / future): CHANGED. Ingest still answers `201 ... 'late': True` for a closed hour and `201 ... 'late': False` for 9999-12-31, so the dodge itself is open. Reconcile now prints it: lines `not billed: late events=1 tokens_in=1000000000 ...` and `not billed: future events=1 ...`; exit 0 by default, `--fail-on-unbilled` exits 1 (checked). Visible now, not prevented.
- RT-money-5 (reconcile with lost closed_hours row): FIXED. Same tamper now gives exit 1 `DRIFT tenant=t1 model=m hour=2026-11-01T10:00:00Z rollup row but its hour is not in closed_hours (rollup=(100000, 0, 1))`; with the closed_hours row put back it reports `tokens_in rollup=100000 events=1000`.
- RT-money-6 (open hours omitted): FIXED. The response has `unclosed_hours`.
- RT-money-7 (total over 2^63): unchanged, NOTE.

## 2. Attacks on the fixes
### RT-money-8  MINOR  `unclosed_hours` is cut at 100 hours with no sign that it was cut
app/pricing.py UNCLOSED_LIMIT = 100, query `LIMIT %(limit)s`; the response carries only the list.
A week has 168 hours; a caller who sees a list of exactly 100 cannot tell whether 100 or 168 hours are missing from `total_micros`, and the hours after the 100th are exactly the ones it cannot see.
Repro (attack script b2.py): 150 open hours for t1 from 2027-01-01, `GET charges?start=2027-01-01T00:00:00Z&end=2027-01-08T00:00:00Z` -> `200`, `len(unclosed_hours) == 100`, no other field.
> Note, 2026-11-11: since PR #65 the charges answer carries `unclosed_hours_truncated`, true when more than `UNCLOSED_LIMIT` (100) hours were open; `unclosed_hours` is still the earliest 100. A range with more than 10,000 lines is also read in pages (`limit`, `cursor`); see docs/billing.md, Paging.
Fix: query LIMIT 101 and add `"unclosed_truncated": true` (or return `unclosed_count`).

### RT-money-9  MINOR  A start or end that is not on an hour boundary gives usage that is in the window but not in the bill, and usage outside the window that is
app/pricing.py charge_lines (`hour_start >= start AND hour_start < end`), UNCLOSED_HOURS uses the same hour_start test, so the two agree and neither flags it.
Rows are whole hours, so for start=10:30 the closed 10:00 hour (holding an event at 10:45, inside the window) is dropped, and for end=10:30 the whole 10:00 hour is included, including usage after 10:30.
Repro (attack script b2.py), hour 10:00 closed with a 1000-token event at 10:45: `start=10:30Z end=11:00Z` -> `200 lines=[] unclosed_hours=[]`; `start=09:00Z end=10:30Z` -> line `('2026-11-01T10:00:00Z', 100000)`.
Adjacent windows split at an hour boundary stay consistent, so no double count if the caller aligns. Fix: 422 unless start and end are on UTC hour boundaries (or document it next to the 366-day cap).

### NOTE  unclosed_hours versus the sum
Held: it lists hours with non-late events not in closed_hours inside the same [start, end) hour_start rule the sum uses, is read before the rows (an hour closing in between is listed AND billed: the safe side), and late-only hours are not listed (correct, they never close an hour). I found no hour that is neither closed nor listed, within the 100 cap.

## What held
- 366 days allowed, 366 days + 1 microsecond is 422; `+14:00` start and `-12:00` end 422 (they span more than 366 days in absolute time).
- Bulk price lookup: hour order, `rows[-1]` is the latest hour, so the "version after the last hour" cut is right; the two-model all-or-nothing path lists both models and sorts them.
- New reconcile queries against my earlier fooling attempts: orphan rows, inflated rollups, lost closed_hours all detected; `--since` and `--hour` scope the new queries too (read, not run).
- Per-hour rollup isolation: read the diff (rollback in close_hour, failed hours logged and skipped); I did not run a poisoned hour (operations lens).

## Not got to
Live run of `--fail-on-unbilled` in compose, statement-timeout effect on a long charges query (15 s default), 1M-row EXPLAIN of UNCLOSED_HOURS (it scans the tenant's events in the range through the occurred_at index only).
> Note, 2026-11-11: a `(tenant_id, occurred_at)` index, `usage_events_tenant_occurred_at` (db/001_usage.sql), now serves UNCLOSED_HOURS.

