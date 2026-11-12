# The build story

The longer account of how the work was run. [how-it-was-built.md](how-it-was-built.md) covers the checks, the review rounds and the limits; this page covers the roles, the gates and what the gates and cross-checks caught.

Claude Code sessions built this in one afternoon and evening, each in its own cockpit (a Zellij session with a lead tab, a
review desk and a merge gate): one lead, two assistant devs, a standing advisor that the lead asked for second opinions,
a review desk (DRC) and short-lived lanes for single jobs. [docs/build-log.md](build-log.md) is the timestamped
record; this is the short version.

[docs/how-it-was-built.md](how-it-was-built.md) explains how AI-written code was checked: the loop each lane was
briefed to follow, the review rounds with their counts, what went wrong and the limits of the method.

**Roles.** The lead wrote the scaffold (schema split by owner, stub routers, a deny-all edge, compose, CI), one brief per
dev, and was the only session allowed to merge. Dev A built usage intake, rollups and the end-to-end test and demo. Dev B
built the price table, the Stripe webhooks, the entitlement record, the edge check and the first README. Later work came
in as lanes, each a branch with its own worktree: reconcile, the delinquent report, the platform adapter, the design
notes, the code tour, the runbook, the testing guide, the hardening fixes and four red-team reviews.

**Gates.** A lane merged only when its branch contained the latest `main` with the tests run on that code, an
independent review desk (DRC) had reviewed it, and the CI check `test` was green. The cockpit's merge
command refuses without a review receipt for the branch, a head that contains the latest `main`, and green CI; commits
added after the review (a merge of `main`, a small fix) and any finding left owed need the lead's written note. Each merge moved `main` and made the next
branch stale, so the lead rebased the early lanes and merged `main` into the later ones, re-ran the tests and merged one
lane at a time, each as its own pull request (`git log --first-parent --merges` lists them).

**Hardening.** The lead ran adversarial agents in rounds: four read-only red-team lanes (money, concurrency, security,
operations), each told to break the code on its own lens and to count a finding only with a command or a failing test
that shows it; a second round of the same four; a narrow third pass on one fix; and a fifth lane that checked every
sentence of the README and docs against the code from a clean clone. Round one found 0 blockers, 9 major and 10 minor. Seven of the majors changed code: a bill that names
every unpriced model, a same-second event that could undo a cancel, a held lock that hung the whole API (now a 503), a paused
database that hung it too (connecting now ends in a 503 after 5 s; a freeze after connecting still hangs, see
[docs/ingest.md](ingest.md)), a request body buffered before the 401 (now capped), and one poisoned rollup hour that
blocked every later hour; two became documented behaviour (how a price change and a back-dated price apply). Round two re-ran each
lane's own repros against the fixed code and attacked the fixes: 0 blockers reported (the operations report gives no count), 3 major, 7 minor. Two majors became code
changes (a cancel older than an applied payment failure was lost; the database lives in a named volume now, because
`docker compose down` followed by `up` had started an empty one); one is written down with its bound in
[docs/ingest.md](ingest.md) (a database that freezes after the connection was made). The review reports are in
[docs/reviews/](reviews/README.md), as written at the time.

**What the gates and the cross-checks caught.**
- DRC found that the hourly token sum can overflow a BIGINT column (now one failed hour that the others survive, and a
  documented limit), a way around the body cap behind a path prefix, and folded fixes of its own into several lanes.
- Reading each other's code, the devs found further defects. Dev A found that every repeated payment-failure webhook
  pushed the grace window forward, which the DRC review of that lane had not flagged (fixed before merge). Dev B found
  a test that could pass by accident because lock waits were counted cluster-wide, and that INFO log lines never reached
  `docker compose logs` because nothing configured logging.
- In the third DRC pass over the entitlements lane, the reviewer folded in a fix of its own and then flagged that same
  fix as opening a fail-open case. The lead asked the advisor, which agreed the guard bought nothing inside
  the one-subscription-per-tenant assumption, and reverted it; the assumption is documented. The advisor also noticed
  that compose published a port next to a public dev secret, which became a loopback-only binding.
- The advisor found that a documented limit was wrong (a cancel for a stored row whose tenant id the new id rule refuses was
  ignored, and a lost cancel is never retried). The cancel fix had its test written first and watched failing.
  DRC folded the daylight-saving fix without a test; the lead added a New York-session test and removed the
  fix once to watch it fail.
- Slips were caught and corrected on every side: the lead mistyped a commit hash in a review job, and named a base that a
  lane did not contain; dev A force-pushed over a merge commit of the lead's, reported eight clean starts of a compose
  change where the true count was seven, and pasted demo output from before its last commit (the lead re-ran the demo
  itself); dev B started throwaway database containers on volumes of live test databases while looking for its own
  (it reported this itself; reads afterwards found no damage; [docs/build-log.md](build-log.md) records it at 19:29).

**Where it hurt.** The review desk could not run the tests until a tracked `scripts/test` gave it a database; messages
to one dev were silently held because the cockpit treats a checkout on a `lane/*` branch as a lane, which cannot read
them; and the review desk takes one job at a time, so with a dozen branches waiting the queue, not the writing, set the pace.
