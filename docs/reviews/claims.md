# Red-team report: claims

> Review report as written at the time. File:line citations refer to the SHA named in the report, not to today's tree.

SHA tested: 843163a1b3e2845d8a84beaa9012d5aebec1f8a4 (fresh clone, README "Run it" followed literally with
COMPOSE_PROJECT_NAME=rt-claims API_PORT=18400 DB_PORT=15400).

Status: PARTIAL. README and docs/billing, ingest, reconcile, design-notes, code-tour, the adapter doc, build-log checked in full.
docs/runbook.md, docs/testing.md, docs/demo.md only spot-checked (in-flight lane); re-check owed against the lead's final SHA.

Counts: MAJOR 0, MINOR 9, NOTE 6.

## Findings

### RT-claims-1 MINOR: code-tour cites wrong lines in app/webhooks.py
docs/code-tour.md cites `@router.post("/v1/stripe/webhook")` at webhooks.py:221, its Depends at :222, and the HANDLERS merge at :189.
Proof: `grep -n` gives the route at 228, Depends at 229, HANDLERS at 196. Lines 221-222 are in the duplicate-handling code.
Wording: 221 -> 228, 222 -> 229, 189 -> 196. (The other 10 code-tour citations hold.)

### RT-claims-2 MINOR: design-notes line citations are stale (8)
Proof (current lines): compose.yaml db healthcheck/restart 15-20 (doc 13-18); api 22-34 (doc 20-32); rollup 36-44 (doc 34-42);
app/ingest.py shared hour lock :141 (doc 138, which is limit_lock_wait); 200-char cap :81, MAX_TEXT at :53 (doc 79, a blank line);
app/rollup.py commit at :83 (doc "78-79", which are the lock and CLOSE_HOUR insert); app/webhooks.py unknown-status lookup :138 (doc 125-126);
AGGREGATE_HOUR rollup.py:30-37 (doc 26-32). Wording: use the numbers above, or cite function names.

### RT-claims-3 MINOR: docs/billing.md:151 "16 of 507 tests fail" under PGTZ=America/New_York
Proof: `PGTZ=America/New_York scripts/test` -> 26 failed, 565 passed (591 total). Normal run: 591 passed, 1 warning.
Wording: "26 of 591 tests fail (at 843163a)"; check whether all 26 are still only test assumptions.

### RT-claims-4 MINOR: append-only caveat worded two ways
README and docs say the trigger does not stop "TRUNCATE and a superuser"; docs/ingest.md says "TRUNCATE or the table owner".
In compose the role `metering` is both owner and superuser; a non-superuser owner can also drop or disable the trigger.
Wording: "does not stop TRUNCATE, or the table owner/superuser (who can disable the trigger)" in all places.

### RT-claims-5 MINOR: README API table omits reachable status codes
Proof: `GET /v1/usage`, `GET /v1/stripe/webhook`, `POST /healthz`, `POST .../charges` -> 405 `{"detail":"Method Not Allowed"}`;
unknown path -> 404. Also deeply nested JSON (about 20k-32k levels, under 64 KiB) -> 400, not 422 as for other bad bodies.
Wording: add a row "any route: 404 unknown path, 405 wrong method" and note the 400 for unparseable JSON.

### RT-claims-6 MINOR: README "How it was built" says the lead merged main into waiting branches; build-log says rebase + force-push
build-log.md lines 13-14 and 43-46 say rebase. `git log` shows 42+ "Merge remote-tracking branch 'origin/main' into lane/..." commits
(mostly later); the early 15:xx lanes were rebased. Wording: "rebased early lanes, merged main into later ones".

### RT-claims-7 MINOR: README:199-200 unsupported process claim
README says a dev started throwaway database containers on volumes of live test databases and that reads found no damage.
Nothing in docs/build-log.md or the repo supports it (added in commit 3a1c036). Wording: cite where it is recorded, or drop it.

### RT-claims-8 MINOR: build-log is stale and named "the timestamped record"
Log ends 19:24; main merged through 20:03. PRs #24-#29 (runbook-recheck, named-volume, limits-docs, cancel-order, healthz-frozen,
readme-update) are not logged. Line 215 says the named volume is "in progress"; it merged as PR #25 at 19:38.
Wording: add the six entries, update line 215.

### RT-claims-9 MINOR: build-log entry times lead the commit times
"merged e2e (PR #5)" is 16:27, merge commit 16:31; "merged rollup (PR #3)" 15:46, commit 15:48; "scaffold at 15:08", commit 15:09.
Wording: use the commit times (`git log --first-parent --merges --format='%cd %s'`).

### RT-claims-10 NOTE: README counts of review rounds
"Twice the lead ran adversarial agents" and "four red-team reviews": the concurrency lens also ran a round three (a89fa21), and this
claims lane is a fifth review. Round totals (round 1: 0 blockers, 9 major, 10 minor; round 2: 3 major, 7 minor) match the four reports.

### RT-claims-11 NOTE: README "Gates" says every lane had a DRC review on its exact head
Not provable from the repo; build-log has no entries for PRs #24-#29, and PR #8 merged without a third review (documented).

### RT-claims-12 NOTE: fixture file names carry the platform name
the adapter's fixture files (5 files). The brief allows the name only in the adapter's module, its tests and its doc
and README pointers (lines 87, 91). Tree text and git history otherwise clean (no commit message carries it).

### RT-claims-13 NOTE: build-log test counts (29, 26, 66, 48, 112, 121, 414, 463, 475, 507) not confirmed
`def test_` counts at merge commits (4, 15, 53, 94, 97) are lower bounds only (parametrization, branch counts). Only the 4 scaffold
tests and the current 591 are confirmed. The "507" in billing.md:151 is stale (see RT-claims-3).

### RT-claims-14 NOTE: shared metering-test-db publishes 5455 on 0.0.0.0
Not in the product; scripts/test only warns. Mentioned for the lead.

### RT-claims-15 NOTE: docs/ingest.md "Verified from the public docs" in the adapter doc cannot be checked offline.

## Checked and held
- Run: `docker compose up -d --build` OK; `scripts/demo.sh` all 7 steps, "demo ok", total_micros 6000; `scripts/test` 591 passed
  in 66.85 s (matches README); `scripts/flood.py` {201: 300, 200: 300}, 0 new tracebacks; `scripts/outage-check.sh` ok;
  apply_schema twice OK; stop api then demo -> "FAIL step 3: expected 200, got 000" as documented; uid 999 and 127.0.0.1 binds hold.
- Status codes reached by request: 200, 201, 400 (4 kinds), 401, 402, 403, 409 (3 kinds), 413 (usage, webhook), 422 (6 kinds), 503 paths
  by reading. 366-day range 200, 367 422. Late flow, duplicate, first X-Tenant-Id wins, unclosed_hours includes current hour.
- CLIs: delinquent (table, --json, --at, --fail-on-expired, bad input exit 2), reconcile (text, --json, --actuals, --hour unclosed exit 2,
  --fail-on-unbilled exit 1, missing file exit 2), rollup one-shot, the adapter's replay/live errors. Output formats match docs, including
  runbook steps 2, 4, 10 formats on this stack.
- 16 relative links resolve; 96 backticked repo paths exist; 12 code-tour and 30+ design-notes citations checked (failures above).
- "Not built" items (livemode, invoice.paid, queue, Stripe calls, dashboard, manifests): grep confirms absent.
- 29 merged PRs match the build-log; round totals match; most merge times match.
- Name/secrets sweep: tree and all history; only fake `whsec_test_only_not_a_real_secret` and POSTGRES_PASSWORD=metering; only the noreply
  email in history.
- Roughly 220 factual sentences, 60 commands run.

## Not got to
- runbook.md full run (steps 11 pause/unpause timings, lock-timeout, bigint overflow hour, `docker pause` 40-thread claim), testing.md
  restart-policy timings and "159 x 500 and 10 x 503", demo.md beyond the run: owed at the lead's final SHA.
- ingest.md "(run)" frozen-database timings; the adapter doc's public-doc URLs (no internet).
- Pytest at historical merge commits for build-log counts.

## Re-check at 82bc2df
SHA tested: 82bc2dfadef8218b472baccadbe8f9a0f47ad92f (same clone, fetched and checked out; stack rebuilt under rt-claims, ports 18400/15400).
Scope: docs/runbook.md, docs/testing.md, docs/demo.md, and the changed paragraph in docs/ingest.md (diff against 843163a read in full).
Result: 0 major, 0 minor, 1 note.

### RT-claims-16 NOTE: demo.md:14 lists `up --wait` as a prerequisite, but the Run section uses `docker compose up -d --build`
`--wait` appears only in the restore recipe (demo.md:38, `up -d --wait db`), not in scripts/ or README. True for the restore, slightly
misleading as a prerequisite for the demo itself. Wording: "docker compose v2 (`up --wait` for the restore recipe)".

### Checked and held at 82bc2df
- `scripts/test`: 591 passed, 1 warning in 74.06 s. The warning text matches testing.md:22 ("Using `httpx` with `starlette.testclient`
  is deprecated; install `httpx2` instead"). The WARNING and 'docker rm -f metering-test-db' line print as testing.md:24-25 says (I did
  not remove the container). "Last line starts `N passed`" holds; "last four rows need a live stack" holds (demo, reconcile, flood, outage).
- `scripts/demo.sh` all steps, "demo ok", total_micros 6000. demo.md's two non-fault WARNING lines (402 `no entitlement`, 401
  `X-Tenant-Id header is required`) appear verbatim in the api log; step 6 prints `closed hour=...` twice as documented.
  `scripts/outage-check.sh` ok; `scripts/flood.py` 600 requests from 150 clients {201: 300, 200: 300}, 0 new tracebacks.
- Runbook commands run as written on the stack: step 2 (curl, psql -x, delinquent --json --at), step 3 (curl, the python charge_lines
  one-liner: output format and total_micros 6000 unclosed [] match; start=5 gives the 422 text; 366-day 422 text), step 3/6 409 for an
  unpriced model, then the new add_price_version one-liner (prints 1, trigger refuses UPDATE of price_versions) and the 409 turning
  into 200 with price_version 1, step 4 (reconcile --hour, --actuals drift line `beyond 100 bps ... diff=-500`, unclosed hour exit 2
  `is not a closed hour`), step 5 queries, step 7 queries and log lines, step 8 queries and log lines, step 9, step 11 curl (200),
  step 12 new piped command: 413 and `{"detail":"request body too large"}`, access line counted in the api log.
- 64 KiB and 1 MiB cap tests exist: tests/test_usage_body_cap.py (lines 13, 95) and tests/test_attack_webhooks.py:138.
- Links in README and the four docs resolve; every backticked repo path in runbook, testing, demo exists.
- Counts: the runbook has no test count; the testing.md count rule matches the run (591). docs/billing.md:151 "16 of 507" is still stale
  (RT-claims-3, other file, fix in flight).

### Not re-run
- The docker-pause timings (runbook step 11 frozen database, 40 threads, restart api 5 s, 50 requests in flight; ingest.md measurements),
  as instructed.
- Runbook step 10 (bigint overflow hour) and the lock-wait display in step 11, the tie-after-cancel and refused webhook log lines
  (step 8 logs checked only for processed lines), and the late-event queries needing a late event (step 5 late rows): formats unchanged
  since 843163a apart from the wording edits above.

## Final check at e603a15
SHA tested: e603a1515b3afa72b027ab6450920a547fc68a45 (diff 82bc2df..e603a15 over README.md, docs, app; stack rebuilt under rt-claims).
Result: 0 major, 1 minor, 2 notes. The first-round minors RT-claims-1 to 9 are fixed (see below).

### RT-claims-17 MINOR: build-log.md:246 says "a script now checks that each cited line holds the code its sentence is about"
No such script is in the tree: `scripts/` holds demo.sh, flood.py, outage-check.sh, test; `git ls-files` has nothing else that checks
citations, and nothing in CI does. Wording: "a script run by hand (not kept in the repo) checked ..." or commit it.

### RT-claims-18 NOTE: build-log claims taken from handoffs cannot be verified from the repo
"588 tests" (dev A clean-clone run, 19:58-20:12), "13 of the 26 failures were not test assumptions" (20:25), "found 8 more wrong
ranges than the lens had listed" (#32; the lens listed 3 + 8), the 19:29 slip details (five volumes, about five seconds each). The
slip itself is now recorded in the log at 19:29 and README links to it, so RT-claims-7 is resolved as "recorded".

### RT-claims-19 NOTE: README says "a narrow third pass on one fix" and a "fifth lane"; the log has the 19:54 entry. Consistent.

### Verified at e603a15
- New York claim: `PGTZ=America/New_York scripts/test` -> 601 passed, 1 warning in 76.45 s (billing.md says "601 of 601 at 02e94c8"; commit
  02e94c8 exists and is the pin). Normal run count is 601 as well (591 + the 10 new tests in tests/test_utc_session.py).
- Reconcile under PGTZ=America/New_York in the rollup container: `DRIFT ... hour=2026-11-11T00:00:00Z` is the UTC hour (the old code printed
  the zone's clock hour). app/db.py: `set_config('TimeZone','UTC',false)` is in the statement that sets statement_timeout, as billing.md
  and the build-log say. billing.md "A connection opened by hand (psql) keeps its own zone" is true by construction (the pin is in db.connect).
- Process wording (README "How it was built", build-log): all 35 first-parent merges are in the log's numbering; merge times for #24 19:29,
  #25 19:38, #26 19:42, #27 19:52, #28 19:58, #29 20:03, #30 20:14 match `git log --first-parent --merges` (US Central). The README
  rebase/merge wording agrees with the log now; the 19:29 slip is recorded.
- Append-only wording: one wording ("does not stop TRUNCATE, or the table owner or a superuser (who can disable the trigger)") in README
  (2 places), ingest.md, runbook (3 places), design-notes; both triggers (usage_events, price_versions) exist in db/001_usage.sql:20-29
  and db/002_billing.sql:14-23.
- README API table now has the 404/405 row: GET /v1/usage -> 405, GET /nope -> 404 observed.
- Citations: all 27 file:line citations added or changed in code-tour.md and design-notes.md (two documents) sit on the quoted text
  (checked by printing each range). RT-claims-1 and 2 are resolved.
- demo.md prerequisite wording edit fine; `scripts/demo.sh` still ends `demo ok`. Links: all relative links in README and docs/*.md resolve
  (none broken).
- Name sweep, final tree: the adapter's name in README.md (lines 87 and 91, pointers), the adapter's module, doc and tests;
  the adapter's fixture file names (5, RT-claims-12 stays a NOTE). All history (`git log -p --all`): 59 matching lines,
  only in those files plus README.md (2); no commit subject or body, author or committer carries it. Authors: 393 commit-email entries
  from the noreply address of the repo owner and 35 from GitHub's own noreply address (merge commits).
- Secrets sweep, tree and all history: only POSTGRES_PASSWORD=metering (compose.yaml, scripts/test, CI) and fake whsec_ values
  (whsec_test_only_not_a_real_secret, whsec_test_fake_secret_for_local_tests, whsec_test_some_other_secret, whsec_test_wrong); no sk_,
  AKIA, ghp_ or private key blocks; no personal email addresses in the tree.

### Not re-run
- docker-pause timings; runbook steps 10-11 tool displays (unchanged since 82bc2df apart from the append-only wording edit).
