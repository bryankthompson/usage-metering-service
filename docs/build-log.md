# Build log

A factual record of how this repo was built by three Claude Code sessions, each in its own cockpit
(a Zellij session with a lead tab, a review desk and a merge gate): one lead and two assistant devs.
Entries are written when the event happens. Times are US Central, 2026-11-10 onward.

## Roles and rules

- **Lead**: owns the scaffold and the contracts (schema, module ownership, stub routers),
  writes each dev's brief, reviews, and is the only session that merges.
- **Dev A**: usage intake and hourly rollups.
- **Dev B**: price table, Stripe webhooks, entitlements, edge check.
- Each dev works on its own branch in its own clone and never merges. Before each merge the branch is
  rebased onto the latest `main` and the tests run on the rebased code. Every branch gets a DRC review
  (an independent reviewer desk) and green CI before it merges.

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
