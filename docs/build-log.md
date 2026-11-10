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
- **15:3x** DRC handback for ingest: merge-ready, no fixes (one finding, dropped by the reviewer's own
  verifier). The lead re-ran the tests on the worktree, opened PR #1 and merged it with `./cockpit merge`
  once CI was green. A reviewer desk handles one job and is then closed, so pricing got a fresh desk. The lead
  mistyped the base SHA of that job line; it was corrected by message before the desk reached the code.
