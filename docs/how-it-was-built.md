# How it was built, and how the AI-written code was checked

Almost all of this code was written by AI coding sessions. This page says how the work was organised and what was done to
find out whether it is right. It states what each check found and what it did not. Times are from `docs/build-log.md`
(US Central, 2026-11-10 onward); pull request numbers are from `git log --first-parent --merges`. Counts of review findings come
from the review reports in [docs/reviews/](reviews/README.md), kept as written; this page restates them. Section 8 says
what the person running the sessions decided.

## 1. The setup

The operator (the person running the sessions) opened them and answered scope questions. One session, the
lead, wrote the scaffold (schema split by owner, stub routers, a deny-all edge, CI), and wrote a brief for each job.
The lead merged PRs #1 to #52 and #72 to #82, plus #84. From 2026-11-11 each dev worked as lead of its own lanes
and merged #53 to #71 through the same gate. The work lived in three working copies of the one repository: the lead's,
and one for each of two devs (dev A:
intake, rollups, end-to-end; dev B: prices, webhooks, entitlements, edge). A standing advisor session gave the lead second opinions. An
independent review desk (DRC) reviewed submitted branches as separate read-only jobs. Later work ran as short-lived lanes, one
job each, and four read-only red-team lanes. Every lane had its own session and its own branch and worktree. Each had a
brief on paper that fixed which files it could touch (build log 17:30). Messages between sessions were treated as data
to check, not as orders.

## 2. The loop each lane was briefed to follow

This is the loop each lane was asked to follow.

1. Brief: what the lane builds, which files it may touch, what it must hand back.
2. Branch: `dev/<name>` locally, pushed as `lane/<name>`.
3. Test first, watched failing: each lane was asked to run the new test against the old code and see it fail for the
   stated reason.
4. Mutation check: break the new rule by one line and see the guarding test turn red, then revert.
5. The full suite (`scripts/test`, a real database, one per checkout) on the code as it will merge.
6. DRC review of that exact head. The tier is `auto`, or `high` for guards, the entitlement path and large diffs
   (build log 15:49, 17:35, 17:40).
7. The CI check `test` is green.
8. The merge gate. The merge command takes a review receipt for the branch or the merging lead's written reason; green CI
   and a current `main` apply to both. Commits added after the review, and any finding left owed, need the lead's written note.
9. One pull request per change: `git log --first-parent --merges` lists every merged pull request.

This is the loop each lane was briefed to follow, and most changes followed it, but not every one. The body-cap fix in
`097e67e` came before its regression test in `b526f4d`. In `2b2bc7b` and `02e94c8`, a test and its fix landed in the same
commit, so their order cannot be read from git; for `02e94c8`, the build log at 20:25 says the ten new tests failed on
the old code.
Other recorded exceptions include the daylight-saving fix folded without a test (section 3), the #21 fold with no test
(section 3), and the `--every` check recorded at 15:46 in the build log, which had no test.

The repo does not store the receipts. What it keeps is the merge commit of each pull request and the build log; for the
review itself the gate rests on the rule above, not on a receipt you can open here.

## 3. How AI-written code was checked: independent layers

Each layer fails in a different way from the others. One example each.

- **Tests that fail on the old code.** Dev A wrote the tests for the lost-cancel fix (#27) first: 2 of 6 failed on the
  previous code. The ten tests added with the UTC session pin (#34) failed on the old code for the stated reasons.
- **Mutation checks.** Before the first handoffs (15:26) dev A broke the race test's rule and dev B the half-up rounding
  rule, and each saw the test go red. The fix for #27 came with three more.
- **A reviewer that did not write the code.** DRC found that the hourly token sum can overflow a BIGINT column after
  about 9 million maximum-size events in one hour (15:39). It was accepted as a documented limit.
- **A fold with no test.** DRC's finding on #21 was a body-cap bypass behind a path prefix. DRC folded a fix, but the
  fold had no test, so the lead added a test that fails when the fold is reverted (19:01).
- **An advisor reading what the tests cannot see.** The advisor noticed a daylight-saving fix had no test that could
  fail, because the default test session runs in UTC (18:00). The lead added a New York session test and removed the fix
  once to watch it fail. A full run under that zone later showed real effects, and #34 pinned every connection to UTC.
- **Forced failures to prove a hang fix.** For #8 the advisor read the diff and ran two forced failures instead of a third
  review: a barrier waiting for a fifth party ended in 31 s with `BrokenBarrierError`, and a blocked wait that never
  matches ended in 6 s. Both edits were reverted before the merge (17:38).
- **A dev reading the other dev's code.** Dev A found that every repeated payment-failure event pushed the grace window
  forward. The DRC review of that slice, at high effort with seven findings, had not flagged it (16:01).
- **Read-only red-team lanes.** Four lenses (money, concurrency, security, operations) attacked the merged code. A finding
  counted only with a command or a failing test, and every one came with a repro (18:20).
- **A lane that checked the words against the code.** A fifth lens read every sentence of the README and docs from a clean
  clone: 0 major and 9 minor findings (stale citations, an old count, a process claim). A script, run by hand and not
  kept in the repo, then checked every citation (#32).
- **Readers with no context.** Three agents with read-only tools, given only a clean copy of the final tree and told nothing
  about who wrote it or why, described what they saw and tried to break the story (21:40). They found stale lines in the
  testing guide, one route (charges) that still took its database connection the way ingest had stopped doing (#38), and
  that the review evidence sat outside the repo, which the next change fixed (#39).
- **Clean-clone runs.** Earlier, Dev A ran the shipped path from a clean clone at `e603a15` (start, demo, outage check,
  flood, data kept across `down` and `up`, emptied by `down -v`, 601 tests, also with `PGTZ=America/New_York`; 20:51).
  Dev B ran the suite from its own clean clone at the same commit and checked 40 citations and every link. Code changed
  after those runs: #38 (`a583885`, `05eec07`) changed the charges connection and its test, and `6eacf22` changed a
  docstring in `app/db.py`. The lead ran from a clean clone of `dea36c8`: 610 tests, the demo and
  the outage check passed (the first demo call came before the API was listening; the rerun passed, as
  `docs/demo.md` says). `dea36c8` was the final-code run of the first public version. Later merges ran the CI suite;
  since #75, CI also brings up the compose stack and runs the demo on every pull request and on main. The flood, data-kept-across-
  `down`/`up` check and New York time zone run were not repeated at `dea36c8`. #45 and #46 changed code afterwards, as
  `docs/build-log.md` (2026-11-11) records.
- **Reviews by models, and a read by the operator.** DRC, the advisor and the red-team lanes were models. On 2026-11-11 the
  operator read five functions on the money and entitlement path as they stood on 2026-11-11 (commit `5dca15d`, section 8).

## 4. The review rounds

Each round's report, and the commit it tested, is in [docs/reviews/](reviews/README.md).

| Round | Lenses | Blocker | Major | Minor |
|--|--|--|--|--|
| Round 1 (on the first merged code) | money, concurrency, security, operations | 0 | 9 | 10 |
| Round 2 (re-run repros, attack the fixes) | the same four | 0 reported (operations gives no count) | 3 | 7 |
| Round 3 (the new delete rule) | concurrency | 0 | 0 | 2 |
| Claims, first pass (words against code) | claims | not counted | 0 | 9 |
| Claims, re-check of the runbook, testing and demo pages | claims | not counted | 0 | 0 |
| Claims, final check (before #38) at `e603a15` | claims | not counted | 0 | 1 |

The reports also list notes, not counted here. The claims reports count major and minor only.

Round 1 had 9 major entries but 8 distinct problems: the same-second cancel was found by two lenses.

| Round 1 major | Result and why |
|--|--|
| A price change inside an hour billed at the old price | Documented: charges rate a whole hour at the price in force at its start, so a version applies from the first hour that starts at or after it. |
| A back-dated price version rewrites a bill already shown | Documented. No bill is stored, so a back-dated version changes later answers. Refusing it was not chosen (section 6). |
| One unpriced model fails a whole range with 409 | Message now names every missing model; still all-or-nothing, which round 2 confirmed. |
| A same-second event undoes a cancel (two lenses) | Fixed: a tie is stale (#16). Round 2 re-ran it and it held. |
| A 600 MB body is read before the 401 | Fixed: a 64 KiB cap on `/v1/usage`, 1 MiB on the webhook (#21). Round 2: 413 in about 1 ms. |
| Ingest waits without a timeout for an hour lock; 40 waiters freeze `/healthz` | Fixed: a 2 s lock wait gives 503 with `Retry-After` (#21). `/healthz` became `async def` (#28). |
| A paused database hangs every request | Fixed before the connection (503 at 5 s, #17). A freeze after connecting is round 2's. |
| One poisoned rollup hour blocks later hours; charges answers 200 with total 0 | Fixed: each hour closes on its own (#17) and charges lists `unclosed_hours`. |

| Round 2 major | Result and why |
|--|--|
| A cancel older than an applied payment failure was stale and lost | Written down as a limit first, then fixed (#27) after the advisor said a stale cancel is recorded as handled and never retried. |
| The database lived in an anonymous volume, so recreating the container lost it | Fixed: a named volume (#25). |
| A database frozen after the connection still hangs the API | Documented limit (section 6). |

## 5. What went wrong, and was written down

- **A force-push over a merge commit (17:45).** A fetch had refreshed the `--force-with-lease` that dev A used, so a push
  overwrote the lead's merge of `main`. The code was the same, but DRC was already reading the old head, so the job was
  sent again.
- **A mistyped base hash.** The lead mistyped the base of a review job at 15:30 and 17:45, and named a base the lane did
  not contain at 18:50. The first was corrected before the desk reached the code, the second within a minute (DRC's reply said the base did not resolve), and DRC refused the third at intake.
- **Throwaway databases on live volumes (19:29).** While checking that an old volume survived, dev B started databases on
  five volumes that held a data directory, three of them live. Afterwards the reads found no errors and the tables intact.
  The rule since: touch only volumes your own compose project created.
- **A wrong hour found only by recounting under another zone.** At 18:00 a New York run failed 16 of 507 tests, judged
  mostly test assumptions. Recounted at 20:25, 13 of 26 failures were real: a drift line named the hour in the zone's
  clock with a literal `Z` (5 hours off), two reports printed an offset instead of `Z`, and a year-one timestamp was a 500.
  Billing was right in every zone. The cause was removed (#34), not documented.
- **A limit that the advisor said was wrong.** A cancel older than an applied payment failure was first documented as an
  accepted limit. At 19:40 the advisor said to fix it, and #27 did, with tests first.
- **A fix the reviewer then flagged (16:05).** DRC folded a fix into the entitlements lane and flagged it as opening a
  fail-open case. The lead reverted it and wrote down the one-subscription-per-tenant assumption.
- **A keepalive idea disproved by measurement (19:40).** TCP keepalive and `tcp_user_timeout` were proposed for a frozen
  database. Dev B measured that they do not end the wait, because a paused container keeps answering probes.

- **A cleanup that ran after a refused merge (21:55).** The lead chained the branch cleanup after the merge command; the
  merge was refused (CI still pending) and the cleanup deleted the branch of the open pull request #38, which closed it.
  The branch was restored from the commit and the pull request reopened within a minute; nothing was lost. The cleanup now
  runs only after the merge reports success.

## 6. Decisions not taken, with the reason

- **Refusing back-dated price versions.** A correction is a legitimate use, so it is documented. Round 2 confirmed the
  code has no floor; a bill is rebuilt, not stored.
- **Refusing the public placeholder webhook secret.** It is fake on purpose and the README says so. The security lens
  still lists it as open.
- **A `Retry-After` on the edge 503.** The 503 `entitlement check unavailable` has none; the 503 for a lock wait has
  `Retry-After: 1` because there a resend is safe. The edge answer means the API cannot read the database, which someone
  has to fix (`docs/runbook.md`, step 11).
- **Leaving the frozen-database hang as a documented limit.** The proposed keepalive options were measured and did not
  help. Recovery is to unpause the database or restart the API (`docs/ingest.md`).
- **A third DRC review of #8.** The advisor read the diff and ran two forced failures instead (section 3).
- **A fix for the half-built data volume after an interrupted first start.** The failure is loud and `down -v` recovers it
  (16:27). An automatic schema step would have meant designing migrations.

## 7. What each layer covered

- Review: models reviewed the code (DRC, the advisor, the red-team lanes) and the operator read five functions on the money and
  entitlement path (section 8). Reviewers found real defects, and a later reader found more, such as the grace-window
  slide.
- The platform adapter (`seekrflow`) is tested against hand-written fixtures and a stand-in server (no API key existed).
- The load check is `scripts/flood.py` (600 requests from 150 clients), with a few one-off red-team measurements.
- The review reports are in [docs/reviews/](reviews/README.md) as written at the time; their line numbers point at the commit
  they tested. The build log restates some counts, and its test counts are from handoffs, not from the repo.
- Through #84, 69 merges carried a DRC receipt and 14 a written reason (docs, CI steps, tests, one index, one compose
  line, a format commit shown AST-equal, a measurement script and a rebase of a head DRC had reviewed at high). The merge
  gate's receipts are kept outside the repo (section 2); the repo keeps each pull request's merge commit and the build log.

## 8. What the operator decided

The code was written and reviewed by models. The operator's work was the method around them: the rules, the review gates, the
red-team lanes and the no-context readers. Below is what they decided, each from their own words in the session, and each
changed what was built.

- **The job and the rules, before any code.** A small usage-metering and billing service, a prototype due the next
  morning; money in integers; no real payment keys; nothing taken from an earlier coding test; the README may claim only
  what the code does.
- **How the work is checked.** A DRC receipt or the merging lead's written reason and a green CI check on every merge, and the repo was private while it
  was built; it was made public on 2026-11-10. The operator also said that the way the sessions work together is part of
  what the build is meant to show, so the build log is kept as it happens.
- **What the platform adapter may be.** It runs on hand-written fixtures and a stand-in server because there was no API key
  (build log 17:20). Until 2026-11-11 its name, `seekrflow`, was kept to its own files (`app/seekrflow.py`,
  `tests/test_seekrflow.py`, `docs/seekrflow.md`). On 2026-11-11 the operator decided to name it everywhere.
- **The bar at the end (17:20).** The build should use the platform of the people it is for, have a way to be tested, be
  robust, and be attacked by adversarial agents at the final review. That became the adapter, the testing guide and the red-team
  lanes.
- **Keeping the other sessions busy.** While the first lanes waited for review, the operator said the other leads could be
  working, directed by the lead; that is why the work ran as many small parallel lanes.
- **A last test of the story (21:36).** Before publishing, the operator asked for agents with no context to read the repo,
  describe what they see, and say whether it tells the story. Their findings led to #38 and #39.
- **The operator's read.** On 2026-11-11 the operator read the five functions on the money and entitlement path, as they
  stood at `5dca15d`: `_store` (`app/ingest.py:136`), `close_hour` (`app/rollup.py:64`, with the ClickHouse copy after the commit),
  `_in_force` and `_rate` (`app/pricing.py:80` and `:88`), `require_entitlement` (`app/edge.py:36`) and `_apply_subscription`
  (`app/webhooks.py:105`). The repo's name stays generic; it has been public since 2026-11-10.
