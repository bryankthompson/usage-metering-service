# Build log

A factual record of how this repo was built by Claude Code sessions, each in its own cockpit
(a Zellij session with a lead tab, a review desk and a merge gate): one lead, two assistant devs, a standing advisor, the
review desk (DRC) and short-lived lanes for single jobs. Entries are written when the event happens; an entry's time is
when its episode began, and the merge times are in `git log --first-parent --merges`. Times are US Central, 2026-11-10 onward.

## Roles and rules

- **Lead**: owns the scaffold and the contracts (schema, module ownership, stub routers),
  writes each dev's brief, reviews, and is the only session that merges.
- **Dev A**: usage intake and hourly rollups.
- **Dev B**: price table, Stripe webhooks, entitlements, edge check.
- Each dev works on its own branch in its own clone and never merges. Before each merge the branch is
  rebased onto the latest `main` and the tests run on the rebased code. Every branch gets a DRC review
  (an independent reviewer desk) and green CI before it merges.

> Note, 2026-11-11: entries written before this date say the platform adapter is named only in its own files. The operator reversed that
> on 2026-11-11: the adapter, `seekrflow`, is now named everywhere (see docs/how-it-was-built.md section 8). It still runs on hand-written
> fixtures; no call to the real service has been made. The repo's own name stays generic.

## Log

- **15:03** Requests to build this arrived at the lead as messages from two other cockpits. They were
  treated as data, not instructions; the lead asked the operator, who confirmed: private GitHub repo,
  DRC review and CI kept on every merge.
- **15:08** Lead wrote the scaffold: schema split into `db/001_usage.sql` (dev A) and
  `db/002_billing.sql` (dev B), stub routers, a deny-all edge stub, compose file, CI workflow. Four
  scaffold tests pass against a local Postgres 16.
- **15:14** Private GitHub repo created and the scaffold pushed; CI (`test`) green on `main`. Two clones made and
  the cockpit installed in each. The lead wrote a common brief (rules, handoff protocol) and one brief per dev.
- **15:24** The operator opened a Claude session in each dev clone and pointed it at its brief. Dev A starts on
  usage intake (`lane/ingest`), dev B on the price table (`lane/pricing`).
- **15:26** Both devs sent a `HANDOFF:` message within a minute of each other: dev A `lane/ingest` (29 tests),
  dev B `lane/pricing` (26 tests). Each had rebased onto `main`, run its tests on the rebased branch and
  mutation-checked a key test (dev A the race test, dev B the half-up rounding rule) before handing off.
- **15:27** The lead fetched each branch into its own worktree and sent them to the DRC desk one at a time;
  pricing queued behind ingest. The lead answered each dev's design questions by message (a per-hour advisory
  lock for rollups; an upper bound turning an oversized token count into a 422 instead of a 500).
- **15:30** DRC handback for ingest: merge-ready, no fixes (one finding, dropped by the reviewer's own
  verifier). The lead re-ran the tests on the worktree, opened PR #1 and merged it with `./cockpit merge`
  once CI was green. A reviewer desk handles one job and is then closed, so pricing got a fresh desk. The lead
  mistyped the base SHA of that job line; it was corrected by message before the desk reached the code.
- **15:32** Dev B handed off `lane/entitlements` (66 tests) stacked on the still-unmerged pricing branch;
  dev A handed off `lane/rollup` (48 tests). Both came with a list of choices the brief did not cover, and the
  lead answered each one (for example: an unknown Stripe status maps to "canceled", so unknown means deny).
- **15:33** Two frictions, both fixed:
  - *Merge gate vs. a moving `main`.* Every merge (or docs commit) on `main` makes the other open branches stale,
    and the gate wants the review receipt on the exact head. So after each merge the lead rebases the next
    branch, force-pushes it, re-runs the tests and sends it back for review. The lead stopped committing the
    build log to `main` while reviews are in flight, for the same reason.
  - *Reviewer could not run the tests.* The pricing review passed (0 findings) but the desk had no database and
    no way to know the test command, so it wrote no receipt. The lead added a tracked `scripts/test` (starts a
    throwaway Postgres, one database per checkout) that the desk finds on its own, and resubmitted.
- **15:34** Dev A reported that the lead's replies were not reaching it. Cause: a cockpit treats a checkout on a
  `lane/*` branch as a lane, and lanes cannot read held messages. Both devs were told to work on `dev/<slice>` locally and
  push to `lane/<slice>`; the lead's earlier answers had also been recorded here, which is how dev A found them.
- **15:36** Pricing merged (PR #2). It took three DRC runs: the corrected job passed on the original head (0
  findings), but `main` had moved, so the lead rebased it; the next run on the rebased head found nothing but could
  not run the tests, so it wrote no receipt; after `scripts/test` was added and the branch rebased once more, the
  last run passed and the merge went through on a receipt for that exact head.
- **15:39** DRC handback for rollup: `fold-ins-owed`. The reviewer fixed two findings itself (a `--every` check,
  lock handling in tests) and left one owed: the hourly token sum can overflow a BIGINT column, which takes about
  9 million maximum-size events in one hour for one tenant and model. Because the merge gate requires the lead to
  name an owed finding, the lead started the standing advisor lane and asked it before merging.
- **15:46** The advisor read the code and agreed to accept the finding, with three corrections the lead had
  missed: a comment claiming no sum could overflow was false, the compose service needed a restart policy for the
  "retried" claim to be true, and the new `--every` check had no test. The lead made those changes in one extra
  commit, said so in the `--lead-checked` text, and merged rollup (PR #3).
- **15:49** Dev B had not yet pushed its rebase of `lane/entitlements`, so the lead rebased it onto `main`
  itself (112 tests pass on the whole tree) and sent it to DRC at high effort, since it holds the signature check
  and the fail-closed edge.
- **15:55** The operator asked for more delegation, since both devs were idle while the review gate (one job at a
  time) and the merge queue (each merge makes the next branch stale) ran serially. The lead gave dev A the next
  slice (`lane/e2e`) stacked on the unmerged entitlements head, with the compose and demo parts handed to dev A's
  own helpers, and gave dev B a README draft. A cheap read-only helper swept the repo for the rules (no company name
  in files or history, no floats in the money path, no real secrets): clean.
- **16:01** Each dev did a read-only review of the other's code. Dev B found three defects in dev A's work (a test
  that could pass by accident because lock waits are counted cluster-wide; a docs claim that the edge makes false;
  late events returning the same response as on-time ones). Dev A found a real defect in dev B's unmerged
  entitlements code: every repeated payment-failure webhook pushed the grace window forward, so a tenant would stay
  allowed through the whole retry period. The lead checked it against the code and sent it back to dev B to fix
  before merge. The DRC review of that slice (high effort, seven findings) had not flagged the grace-window defect.
- **16:05** Dev B's fix for the grace-window slide (grace runs from the first failure, deleted subscriptions
  always cancel) went back to DRC. The DRC desk refuses a third review of one lane unless the lead says so; the
  lead approved it, since the change was new guard logic. The third pass found nothing against the new code, folded
  one more fix of its own, and then flagged that very fix as opening a fail-open case under out-of-order events.
- **16:20** The lead asked the advisor lane again. It agreed the guard bought nothing inside the prototype's
  one-subscription-per-tenant assumption and cost a fail-open inside it. The lead reverted the guard (back to the
  code DRC had reviewed), documented the assumption, named every owed finding in the merge text, and merged
  entitlements (PR #4). All four core slices are now on `main`.
- **16:21** The advisor also flagged a compose detail nobody had raised: the API port was published on all
  interfaces next to a public dev webhook secret, so anyone who could reach the host could forge a subscription
  event. Dev A was asked to bind the published ports to 127.0.0.1 in the end-to-end slice.
- **16:21** Dev A corrected its own earlier report on a compose healthcheck change (it first said eight clean
  starts; the true count was seven) before the lead repeated the number anywhere.
- **16:23** Dev A handed off `lane/e2e` (rebased onto `main` after entitlements; 121 tests) and, in a second
  message a minute later, corrected its own handoff: the demo output it had pasted was a template made from runs
  before its last commit, and after that commit it had only looked at the tail of two runs. The lead ran the demo
  itself on the exact head in a throwaway compose project (all seven steps, total 6000, `demo ok`, ports on
  127.0.0.1 only) rather than rely on the pasted text.
- **16:24** Dev A's read-only review of the docs found three sentences that were looser than the code (what
  `GRACE_PERIOD_HOURS` affects, the charges route's edge status codes, which events a rollup accounts for). The
  lead checked each against the code and passed them to dev B for the README branch, as their own commit.
- **16:27** DRC handback for e2e: 3 found, 1 folded (the demo's step 6 checked database-wide rollup counts, so
  it would fail when another tenant's hour closed in the same run; it now checks its own hour), 1 dismissed, 1 owed
  (an interrupted first start leaves a data volume Postgres accepts without the tables). The lead asked the
  advisor, which agreed to accept it: the failure is loud and `docker compose down -v` recovers it, while an
  automatic schema step would have meant migration design. The lead re-ran the demo on DRC's head (twice; the
  second run picked the previous hour), added two notes to `docs/demo.md` (the half-built volume, and a demo run
  seconds after `up` that can hit a not-yet-listening API) and merged e2e (PR #5).
- **16:34** Dev B handed off `lane/readme` (5 commits on `main`, docs only, 121 tests). It had run `docker compose up`
  and the demo itself on a throwaway project, twice, plus the case with nothing listening, and sent a table of every
  README claim with the file and line that backs it. It corrected two line numbers in that table before the lead read it.
- **16:47** The lead sent the handoff to dev A for a read-only line-by-line review, fetched the branch into a
  worktree and re-ran the tests on it (121 pass). A cheap read-only helper checked the README against the code
  without using dev B's table: 58 of 59 claims matched. The one loose sentence was "an older event never overwrites
  newer state", which has one narrow documented exception (an older payment failure can open a missing grace
  window); the README now points to `docs/billing.md` for it. The lead wrote the "How it was built" section and
  checked each count against this log, which caught two errors in the draft: two of dev A's slips had been
  credited to two different devs, and the merge gate was described as enforcing more than it does. The lead also swept
  files and history for the company name, real secrets and floats in the money path: clean.
- **16:52** Dev A sent back its review of the README: 6 findings, wording and caveats, no wrong status code or number,
  after running the Run-it steps on a throwaway project of its own. The lead checked each against the code and all six
  held: nothing enforces test mode (`livemode` is never read), `scripts/test` published its database port on every
  interface (the same hole already closed for compose), "all answers are 200" left out the 400 and 500 cases, the
  payment-failure tenant lookup went unmentioned, `invoice.paid` is unhandled, and "never rewritten" ignored TRUNCATE.
  The lead fixed the README wording and Not built list, bound the test database to 127.0.0.1 and corrected the
  rollup docstring dev A wanted fixed, all in the README lane: a separate branch would have cost another review round.
  The tests pass (121), run against a fresh container on the new binding.
- **16:56** The operator asked for the build to be hardened before it ships (a clear way to test it, adversarial agents at
  the final review) and for the devs to work while the README is in review. The lead briefed each dev to attack the
  other's slice: dev A the Stripe webhook, entitlement, edge and pricing code, dev B the ingest, rollup and compose code,
  each with a probe list, a rule to fix real defects with a failing-first test, and a handoff time.
- **17:16** DRC handback for readme: 3 found, 1 folded, 1 owed. The fold made `scripts/test` refuse to run against an
  older shared test container that publishes its port beyond loopback; the owed finding was that this check passes
  when `docker port` prints nothing. The lead asked the advisor, which advised turning the refusal into a one-line
  warning: the container holds a fake test database and can be recreated, and a refusal would have stopped both devs'
  test runs in the middle of the hardening round. The lead made that change in one extra commit, ran `scripts/test`
  against the old container (warning printed, 121 tests pass) and a loopback one (no warning), and accepted the
  owed finding as an advisory check that can still miss. The old container is removed once the devs hand off.
- **17:19** The README lane merged (PR #6) after its review, the lead's extra commit and a clean CI run.
- **~17:20** The operator asked that the build use the inference platform the adapter is written for, and be robust and testable, with adversarial
  agents at the final review. The lead put one scope question to the operator (what "their features" means; whether the
  company name may appear; whether an API key exists) and recorded the answers: the adapter module, its tests and its docs
  page may carry the platform name, everything else stays generic; there is no API key, so no call to the real service has
  been or will be made. The lead did not ask the operator the same question again.
- **17:26** The reconcile lane merged (PR #7): a read-only check that recomputes the rollups with independent SQL and
  compares them to the stored rows and to an optional file of actual figures (exit 0 clean, 1 drift, 2 bad input). The
  first lane for it (a Codex session) stalled at a trust prompt that the lead could not answer, so the lead closed it and
  restarted it as a Claude lane.
- **17:30** The lead started three more lanes in parallel with the devs' hardening: a delinquent-accounts report, a
  platform adapter, and a design-notes page, each with a written brief that fixes the files it may touch. The standing advisor
  ranked the scope before the briefs went out.
- **17:35** The delinquent report merged (PR #9). DRC reviewed it at high effort because it moves the edge's grace check into a
  shared helper: 0 findings.
- **17:36** DRC handed back the adapter lane (platform adapter): 6 found, 4 folded, 1 owed (the file it had added to save a
  live response was overwritten without a check). The lead found that the saved file was also untested, was named from the
  remote response id and had left a stray file in the repo during the test run, so the lead made saving opt-in and added two
  tests instead of guarding the overwrite.
- **17:38** The attack-ingest lane merged (PR #8). DRC's second look repeated two owed ids on lines that already held the fix.
  The lead did not run a third review: the advisor agreed to read the diff and run two forced failures, so that a hang fix would
  be shown to work and not only to pass. A barrier waiting for a fifth party ended in 31 s with BrokenBarrierError; a
  blocked-wait that never matches ended in 6 s. Both edits were reverted and the tree was clean before the merge. The merge
  note records the repeated ids as a stale echo that DRC could not confirm, and says that it is the lead's judgement.
- **17:40** Dev A handed off the billing attack lane: 8 commits, 414 tests, each new rule mutation-checked. Dev B reported that
  INFO log lines from the `app.*` loggers never reach `docker compose logs api` because nothing configures logging, so a
  duplicate or ignored webhook leaves no trace on our side. The lead merged main into the lane (463 tests pass) and sent it
  to DRC at high effort. The logging fix is a separate small lane after it, so the review of a large diff does not restart.
- **17:45** Dev A pushed the billing attack lane again with the logging fix included (475 tests), and in the same command
  overwrote the lead's merge-of-main commit on that branch: a fetch had refreshed the force-with-lease it was using. No
  content was lost (the new head is the same code rebased onto main), but DRC was already reviewing the old head, so the
  lead re-sent the job for the new range. The lead also made a slip in that message, mistyping the base hash, and sent a
  correction within a minute. Both are recorded here: the typo was caught by DRC, whose reply said that the base it was given did not resolve.
- **17:50** The adapter lane merged (PR #10) with a merge note that names the owed id (the spool file), says it was removed
  and not fixed, and says that the repeated ids from DRC's second look were not confirmed. The advisor asked for a test of the
  redirect rule that had none: two stand-in servers, a 302 from the first to the second; with redirects switched back on the
  test fails because the key reaches the second server.
- **18:00** DRC handed back the billing attack lane: 6 found, 1 folded (price versions compared in local wall-clock time,
  which bills a daylight-saving fall-back hour at the wrong price), 4 owed. The lead fixed one (a price version no Python date
  can hold broke every bill; test first, failing with the database's own error), and the advisor said that a documented
  limit was wrong for another: a cancel for a stored row whose id the new rule refuses was ignored, and a lost cancel is
  never retried, so the tenant would stay active. Both fixes have tests written first. The advisor also found that the folded
  fix had no test, since the default test session runs in UTC where the bug cannot show; the lead added a New York-session test
  and removed the fix once to watch it fail. Running the whole suite under a New York session zone then failed 16 of 507 tests:
  mostly tests that compare local clock hours (a test assumption), and one real effect (a year-1 event is a 500 west of UTC).
  It is written down in `docs/billing.md` as a limit.
- **18:00 and 18:07** The testing guide (PR #12, 4 script fixes folded by DRC, none owed) and the billing attack lane (PR #11)
  merged. The design notes (PR #13, 18:14) and the code tour (PR #14, 18:19) followed.
- **18:20** The lead started four red-team lanes (money, concurrency, security, operations) on the merged code. Each is read-only
  toward the product, must prove a finding with a command or a failing test, and reports by 20:15; a fifth lens, claims against
  code from a clean clone, runs after the docs land.
- **18:20 to 18:35** The four red-team reports came back within about 15 minutes of the launch: 0 blockers and 9 major findings,
  every one with a repro (in-process attacks on the real database, or a live compose stack). They were: a price change inside
  an hour billed at the old price while the docs said otherwise; a back-dated price version changing a bill already shown; one
  unpriced model failing a whole range; a same-second event undoing a cancel (found independently by two lenses); a
  usage body of 600 MB buffered before the 401; ingest waiting without a timeout for an hour lock, so 40 waiters froze
  `/healthz`; a paused database hanging every request instead of answering 503; and one poisoned rollup hour blocking all later
  hours while charges returned 200 with a total of 0. The lead sorted them into fixes (tie rule, lock timeout, body cap,
  database timeouts, per-hour isolation in the rollup, a flag for unclosed hours, a range cap) and docs that say what the
  code does (the price takes effect at the start of the hour; no bill is stored; charges cover closed hours only), and sent
  them to the devs and one new lane as separate small branches, so that review did not queue behind one large diff. The
  lead did not accept two suggestions: refusing back-dated prices (a correction is a legitimate use; documented instead) and
  refusing the public placeholder webhook secret (it is fake on purpose and the README says so).
- **18:32** The runbook merged (PR #15). Dev B had run each of its commands on a live stack.
- **18:37 to 19:24** The fixes went through DRC and the gate one small branch at a time: the cancel tie rule (#16, DRC 0 found,
  18:37), the database timeouts and per-hour rollup isolation (#17, DRC at high, 2 found, 1 folded: the statement timeout is
  set on the session, so options in the connection URL survive; 1 owed, written down as a limit, 18:46), the lock timeout and
  the request body cap (#21, DRC at high, 3 found, 2 folded: the cap is looked up by the route path behind a prefix, and a
  test helper always closes its connection; 19:01), reconcile's blind spots (#18, 19:06), the charges guards (#22, 19:09),
  the container user and a pinned uv (#19, 19:12), a corrected timing sentence (#20, 19:20) and a quoting fix in
  reconcile's output (#23, 19:24). DRC's finding on #21 was a bypass behind a path prefix; the lead added the test that
  fails when the fold is reverted, because the fold had none.
- **18:50** A slip on the lead's side: the DRC job for #21 named a base that the lane did not contain (the lane held an
  older main), and DRC refused it at intake. The lead merged main into the four waiting lanes and resent the job.
- **19:12 to 19:23** Round two of the red-team: the four lenses re-ran their own repros against the fixed code and attacked
  the fixes. 0 blockers reported (the operations report gives no count). Fixed from it: model and tenant names in the reconcile report (#23). Written down as limits and not
  fixed: a charges range that cuts an hour, the 100-hour cap of `unclosed_hours` (no truncation flag), a frozen database
  mid-query, the exclusive lock during a very large close. The shared clock for invoice and subscription events (a cancel
  older than an applied payment failure was stale) was first written down as a limit and then fixed (see 19:40). Two of the three majors were then fixed in code: the
  operations lens found that Postgres data was in an anonymous volume, so `docker compose down` then `up` started an empty
  database (named volume, #25, 19:38); the concurrency lens found that a cancel older than an applied payment failure was
  lost (see 19:40).
- **19:29** Dev B disclosed a slip: while checking that an old Postgres volume survives, it had started throwaway database
  containers on five Docker volumes that held a Postgres data directory, three of them volumes of live test databases, for
  about five seconds each. Reads afterwards found no errors and the tables intact; the lead told it to touch only volumes its
  own compose project created.
- **19:24 to 20:14** Docs and the last code: the runbook re-run on a live stack with four new failure entries (#24, 19:29),
  the named database volume (#25, 19:38; DRC folded a `--wait` into the migration step), the limits found in round two written
  down (#26, 19:42), the fix for the lost cancel (#27, 19:52, DRC at high, 0 found), `/healthz` made `async def` (#28, 19:58),
  the README (#29, 20:03) and a stranger's-read pass over the runbook, testing and demo pages (#30, 20:14).
- **19:40** The standing advisor, asked whether to document or fix the lost cancel, said fix it: a stale cancel is recorded as
  handled and never retried, and no second clock is needed, because a canceled subscription id is not reopened. A delete of
  the stored subscription id now skips the stale check and the stored clock keeps the larger value; a delete of another
  subscription id is still judged by the clock. Dev A wrote the tests first (2 of 6 fail on the previous code) and three
  mutation checks. The advisor also said that `/healthz`, a plain function, shared the worker pool with the database routes,
  which is why it stopped answering with 50 requests parked on a frozen database; dev B measured that the proposed TCP
  keepalive options do not end the hang (a paused container keeps acknowledging probes) and wrote the measurement down.
- **19:54** A third, narrow concurrency pass attacked the new delete rule: 0 blockers, 0 major, 2 minor (a plan-swap sequence
  that cancels, which the one-subscription assumption already documents, and a re-subscribe made stale by the clock, which
  fails closed).
- **19:58 to 20:12** Two sessions that had not written the docs read them: dev A ran the shipped path from a clean clone
  (compose up, demo, outage check, `down` then `up` keeps the data, `down -v` empties it, 588 tests) and dev B read the
  runbook, testing guide and demo page as a stranger (7 steps that could not be followed, 5 places where two pages disagreed,
  4 overclaims); both lists were applied. A fifth review lane then checked every sentence of the README and docs from a clean
  clone: 0 major and 9 minor findings (stale citations, an old count, wording that differed between pages, a process
  claim), all fixed or recorded here.
- **20:23** The claims lane's re-check of the runbook, testing guide and demo page against the merged head found 0 major and 0
  minor findings and 1 note (a prerequisite sentence in the demo page, fixed). Its first report was applied in three small docs
  branches: the citations in the code tour and design notes (#32: a script, run by hand and not kept in the repo, checked
  every citation against the code its sentence is about, and it found 8 more wrong ranges than the lens had listed), the
  build log and the README's process wording (#31), and one wording for what the append-only triggers do not stop (#33: TRUNCATE, or the table owner or a
  superuser, who can disable the trigger; run on a scratch database for both tables).
- **20:25** Dev A ran the whole suite under a New York database session zone to recount the limit written at 18:00 and found
  that 13 of the 26 failures were not test assumptions: reconcile named the hour of a drift line in the zone's clock with a
  literal `Z` (5 hours off), charges and the delinquent report printed the zone's offset instead of `Z`, and a year-one
  timestamp was a 500. Billing was right in every zone. The lead chose to remove the cause and not to document it: `db.connect`
  now sets the session zone to UTC in the statement that sets the statement timeout (#34). The ten new tests failed on the old
  code for the stated reasons, the whole suite passes unchanged under both zones, and the old limit paragraph in
  `docs/billing.md` was rewritten.
- **20:44 to 20:53** Final checks on the code as merged at `e603a15` (when this was written, the commits after it changed
  documentation only; #38 changed code later, see 21:36 to 22:05). Dev A
  ran the shipped path from a clean clone: start, demo, outage check, flood, data kept across `down` and `up`, emptied by
  `down -v`, 601 tests, and 601 again under a New York session zone. Dev B ran the same suite from its own clean clone, 40
  citations and every link. The claims lane's final check found 0 major and 1 minor (the build log said a script checked
  the citations while none is in the repo; reworded in #36, which also says in the runbook how a wrong price version is
  superseded). The lead ran the suite, the demo, the outage check and the adapter replay once more on `c9ddf6b`.
- **21:08 to 21:36** The operator said the interviewers may care how the code was built. Dev B wrote
  [how-it-was-built.md](how-it-was-built.md), checking each count and pull request number against git and the review
  reports; DRC checked it again and asked for this entry and the one above.
- **21:36 to 22:05** Before publishing, the operator asked for agents with no context to read the repo and say what story it
  tells. Three read-only agents (a README-only first look, a full read, a skeptic) read a clean copy of the final tree. They
  recovered the design, the review process and the honest limits, but rated the process story weak because its evidence
  (the review reports) was outside the repo, and they found stale lines in the testing guide, a docstring that contradicted
  the pricing rule, a README sentence that overclaimed (a paused database), and one route (charges) that still opened its
  connection in a dependency. Fixed: the charges connection (#38) and, in one docs change, the five review reports in
  `docs/reviews/`, a "Start here" block, the stale lines and three limits written down (#39). A slip is recorded in
  `docs/how-it-was-built.md` section 5 (21:55).
- **22:05 to about 22:25** A second round of cold readers, with no context, read the tree after #39 and #40. Their findings
  were fixed in #41: a stale health-check comment in `app/db.py`, a hard-coded pull-request count removed from the docs,
  and a clarification that the app connects as the database superuser. The operator decided to keep the generic repo name
  and made the repo public on 2026-11-10 at about 22:25 CST. The state of main at this entry is `dea36c8`. The lead ran,
  from a clean clone of `dea36c8`, the 610 tests, the demo and the outage check, and all passed. The first demo call came
  before the API was listening ([the demo guide](demo.md) says to give it a few seconds); the rerun passed.
  This is the run of the final code; the `e603a15` runs came before #38.
  The platform adapter has never run against the live service: there was no API key, so it runs on hand-written fixtures.
  No person has read the diff, and the operator's read of the money and entitlement code has not happened.
- **2026-11-11** Two more features merged, then a walk of the whole app. #45 `feat: optional ClickHouse mirror of closed
  rollups, with backfill and reconcile export` merged at `91d0b05` (main `250b278`): `app/mirror.py`, the compose profile
  `analytics`, [the ClickHouse guide](clickhouse.md). DRC reviewed three rounds (the third forced by the lead): QR-3, QR-4,
  QR-5, QR-7 and QR-8 were folded in, the owed QR-1, QR-2 and QR-6 were fixed by the lane in `d144eff`, QR-9 was dismissed;
  620 tests at `d144eff`. It was run by hand once on a real ClickHouse before the last two fix rounds; after that only
  stand-in server tests ran. #46 `feat: scripts/up plays a three-tenant scenario...` merged at `1d0c3f5` (main `26540ac`):
  `scripts/up`, `scripts/scenario.py`, `scripts/stand_in_inference.py` and an api healthcheck in `compose.yaml`. DRC
  reviewed three rounds (the third forced): QR-1 to QR-4 were folded in, QR-5 was fixed by the lane in `1d0c3f5`, QR-6 to
  QR-8 were dismissed with reasons; 633 tests at `1d0c3f5`. Its first CI run stayed "queued" with no jobs for 32 minutes;
  closing and reopening the pull request started a new run, which passed. `scripts/up` on merged main took 13.1 s (compose up
  9.6 s, scenario 3.3 s); an earlier run on the lane head took 25.7 s with a cold image build. The charges matched the
  script's own arithmetic (6038 and 5584 micro-USD) and reconcile drift was 0. Dev B then walked the whole app on `250b278`
  with its own ports and database: the demo, the flood (600 requests, 150 clients), the outage check, 55 of 56 README
  status-code probes (the miss was its own probe), ClickHouse drift, backfill and outage recovery, and 620 tests. It found
  four documentation gaps (the start-up wait, the nesting depth behind the 400, the adapter's error for embeddings without
  `--model`, two items numbered 8), fixed in the `lane/doc-gaps` change. The adapter runs on hand-written fixtures because there is no key
  (access is gated behind "request access or contact sales"). The operator read five functions (see section 8 of
  how-it-was-built.md).
- **2026-11-11 (later)** The operator said they read five of the money and entitlement functions as they stand at `5dca15d`:
  `_store` (`app/ingest.py:136`), `close_hour` (`app/rollup.py:64`), `_in_force` and `_rate` (`app/pricing.py:80` and `:88`),
  `require_entitlement` (`app/edge.py:36`) and `_apply_subscription` (`app/webhooks.py:105`). This supersedes the 22:05 entry's
  "has not happened" for those functions only. The adapter still runs on hand-written fixtures
  and a stand-in server (see section 8 of how-it-was-built.md).
