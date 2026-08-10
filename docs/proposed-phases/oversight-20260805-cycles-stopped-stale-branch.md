# Oversight 2026-08-05: monitor pipeline appears to have stopped ~30h ago; host is running a stale, unmerged branch

**Type: investigation-only.** This is not a code-bug fix. The evidence below is all
inferable from the repo + gist; the root cause (why the host stopped producing
fresh cycles) requires hands-on-keyboard access to Ellingtons-MacBook-Pro-4 that
this session does not have.

## What's real

Current gist (fetched 2026-08-05):

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
alive: True last_success_hours=0.7993
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

Two open PRs already target the two named reasons: #9 (merged 2026-08-04, phantom
exposure / 0-orders fix) and #10 (open, stale-cycle-ledger fix for the
172.3h-old-phase reason). **This doc is not a duplicate of either** — it's a
different finding: the numbers above are frozen, not live.

Evidence the whole snapshot is a stale, one-time capture rather than a fresh read:

1. `pending_prs: 3,4,5,6,7,8` — PRs #9, #10, #11 already exist (created
   2026-08-04). A monitor run today would list them. It doesn't.
2. `last_cycle` timestamp (2026-08-03T23:37:22) is *identical* to the one quoted
   in PR #9's 2026-08-04 check-in comment, which was itself already a day old at
   the time. No new `scheduled_demo_cycle` finish has been recorded in at least
   ~30h.
3. `host commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a` is the exact tip of
   **`origin/phase-25-oversight`** (dated 2026-08-01 20:45 -04:00) — confirmed via
   `git log -1 --format=%H origin/phase-25-oversight`. That branch is **PR #8,
   still open/unmerged**, and does *not* contain the PR #9 phantom-exposure fix
   or PR #11 (`git merge-base --is-ancestor b430ce6... origin/phase-25-oversight`
   → NO). So the host is several days stale and is missing a bugfix that's
   already merged to main.
4. In `src/nbabot/monitor.py` (`origin/phase-25-oversight`, `_cycle_summary()`,
   ~line 136-176), `hours_since_last_successful_cycle` is computed as
   `now - success_dt` **at the time the monitor script runs**, then baked into
   the gist. `0.7993h` next to a `last_cycle` of 2026-08-03T23:37 is only
   consistent with "now" (at generation time) being ≈2026-08-04T00:25 ET — i.e.
   this snapshot was generated once, around then, and never regenerated since.

Put together: the monitor's own cron/launchd job appears to have stopped firing
around **2026-08-04 00:25 ET**, roughly 30+ hours before this check. Everything
downstream (severity, reasons, "alive") is a frozen echo of that one run, not a
live read of current bot health.

## Suspected root cause(s)

- `src/nbabot/agents/meta_check.py` (same branch) is designed to catch exactly
  this: if `data/monitor_heartbeat.txt` is >3h stale it fires a
  `🔴 URGENT: meta-check monitor failure` alert directly via Telegram/osascript
  (`run()`, heartbeat check ~line 90-97). But PR #8's own description says the
  meta-check launchd agent is a **post-merge, manual step**
  (`launchctl load scheduler/meta-check.plist`). Since PR #8 is still unmerged,
  it's very likely that job was never installed on this host — so the one
  watchdog built to catch "the monitor went silent" was itself never turned on.
- Separately, and independent of the above: the host's local checkout being 4
  days behind main (`origin/phase-25-oversight` vs `origin/main`) means even a
  fresh cycle would still carry the pre-fix phantom-exposure bug that PR #9
  already resolved on main. The merged fix has not reached production.

## Proposed fix

None of this is a code change to make blind. This needs verification on the
actual host first:

1. Confirm Ellingtons-MacBook-Pro-4 has been awake/online continuously since
   2026-08-03 (sleep/lid-close would explain a clean stop with no crash trace).
2. `launchctl list | grep nbabot` (or `com.nbabot.monitor`) — is the monitor
   agent loaded and running on schedule at all?
3. On the host, `cd` to the repo and check the *actual* current local branch/HEAD
   and `git status` — does it match `0d9ae5d...`/`phase-25-oversight`, or has it
   moved and the gist is just stale for a different reason (e.g. `gh` auth
   expired inside `_pending_prs()`, or the gist push step is failing silently)?
4. Inspect `data/scheduled_cycle_runs.jsonl` and `data/monitor_heartbeat.txt`
   directly for real recent timestamps — this session cannot read host-local
   data files, only the repo and the public gist.
5. Once confirmed alive: merge/rebase `origin/main` (which has PR #9 and #11)
   into whatever branch runs in production, so the phantom-exposure fix is
   actually live. Then merge PR #8 and load both
   `scheduler/monitor.plist` and `scheduler/meta-check.plist` per its own
   "after merge" checklist, so the watchdog is actually active going forward.

If, after checking the host, cycles turn out to genuinely be running and only
the *gist* delivery is broken (e.g. `_deliver_gist` in `monitor.py` failing
silently, ~line 200-216), that's a smaller, real code bug worth its own
follow-up phase — but confirm host state first; don't guess.

## Tests to add

Once root cause is confirmed as "monitor stopped running" (not a gist-delivery
bug), add a monitor-side self-staleness guard so this can't hide silently again:
a test asserting `evaluate_severity()` escalates to `red` when the *snapshot
generation itself* is being read back more than, say, 12h after it was written
(compare a `generated_at` field against wall-clock at gist-fetch time, not just
at generation time) — today `hours_since_last_successful_cycle` is only ever
evaluated relative to "now" *inside* the same run that computed it, so a run
that never happens again can never re-evaluate its own staleness.

## Verification checklist

- `.venv/bin/pytest -q` — baseline, no code changed here.
- Real host check (manual, not automatable from this session): confirm launchd
  agent state and current local git HEAD.
- After remediation: re-fetch
  `https://gist.githubusercontent.com/ellingtonfagan/01680ea910e51feb0bb95183f841efcb/raw`
  and confirm `pending_prs` includes 9/10/11+ and `last_cycle` has a timestamp
  from the current day.
- `git diff --stat` against this branch: docs-only, 1 file added, 0 lines of
  application code touched.

## Guardrails

No commit/push beyond this doc. No live env vars set. No live orders. Do not
edit live gates (`live_execute.py`, live gate env vars, `risk.py` thresholds,
`sizing.py`, `MIN_EDGE` values).

## Update 2026-08-05T12:26 UTC — one oversight cycle later, still frozen

Re-fetched the gist 8h after this PR was opened. It is **byte-identical** to
the snapshot quoted above:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

Same `last_cycle`, same host commit (still the unmerged `phase-25-oversight`
tip), same `pending_prs` list missing #9/#10/#11/#12. Two things this confirms
that the original doc could only infer:

1. **The snapshot itself has not regenerated even once in this 8h window.**
   This isn't just "the monitor was stopped as of 2026-08-04 ~00:25 ET" — it's
   now confirmed stopped through at least 2026-08-05T12:26 UTC, i.e.
   `now - last_cycle` ≈ **32.8h** and climbing, not a one-time stale read.
2. Nothing about the host state changed between the last two oversight checks
   (this one and the one that opened this PR) — whatever stopped the cron/
   launchd job on 2026-08-03/04 is still stopped. It did not self-recover.

No new root cause found beyond what's above — this is confirmation, not new
diagnosis. The investigation checklist and guardrails are unchanged.

## Update 2026-08-05T20:09 UTC — third check, ~40.5h and still frozen

Re-fetched the gist again, ~8h after the previous update. Still **byte-identical**:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **40.5h** (was 32.8h at the last check, 8h prior — the gap
grew by exactly one oversight interval, confirming zero new cycles ran in
between). `pending_prs` still stops at 8, still missing #9 (merged 2026-08-04),
#10, #11, #12. Three consecutive 8-hourly checks now show the identical frozen
snapshot — this is not measurement noise, the host has been non-producing for
at least three full oversight windows.

No new root cause beyond what's already documented above. No code changed on
this branch. Still investigation-only pending hands-on-host verification.

## Update 2026-08-06T04:09 UTC — fourth check, ~48.5h and still frozen

Re-fetched the gist again, ~8h after the previous update. Still **byte-identical**:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **48.5h** (was 40.5h at the last check, 8h prior — the gap
grew by exactly one oversight interval again). `pending_prs` still stops at 8.
Four consecutive 8-hourly checks now show the identical frozen snapshot,
spanning two full calendar days with zero new cycles recorded. This has moved
well past "confirm it's not noise" — the host has been non-producing since
2026-08-04 ~00:25 ET and nothing in the repo or gist indicates it has been
touched since. This session still cannot distinguish "host asleep/offline" from
"cron/launchd job removed" from "process running but gist delivery broken"
without hands-on access; the investigation checklist above is unchanged and is
the fastest way to tell them apart.

No new root cause beyond what's already documented above. No code changed on
this branch. Still investigation-only pending hands-on-host verification.

## Update 2026-08-06T12:26 UTC — fifth check, ~56.8h and still frozen

Re-fetched the gist again, ~8h after the previous update. Still **byte-identical**:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **56.8h** (was 48.5h at the last check, 8h prior — the gap
grew by exactly one oversight interval again, the fifth in a row). `pending_prs`
still stops at 8. Five consecutive 8-hourly checks now show the identical frozen
snapshot, spanning **~2.4 calendar days** with zero new cycles recorded. There is
still nothing in the repo or gist that distinguishes "host asleep/offline" from
"cron/launchd job removed" from "process running but gist delivery broken" —
that requires the hands-on-host checklist above, which this session cannot run.

No new root cause beyond what's already documented above. No code changed on
this branch. Still investigation-only pending hands-on-host verification. Given
five straight confirmations with no change, escalating this to the user directly
rather than only updating this PR silently.

## Update 2026-08-06T20:08 UTC — sixth check, ~64.5h and still frozen

Re-fetched the gist again, ~7.7h after the previous update. Still **byte-identical**:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **64.5h** (was 56.8h at the last check — the gap grew by
exactly one oversight interval again, the sixth in a row). `pending_prs` still
stops at 8; PR #9 and #11 have since merged (#9 on 2026-08-04, #11 on
2026-08-04) but the gist has never picked that up, consistent with the host
not having run a single fresh cycle since. Six consecutive 8-hourly checks now
show the identical frozen snapshot, spanning **~2.7 calendar days**.

`phase-25-oversight` (PR #8, which contains `meta_check.py`'s heartbeat
watchdog) is still open/unmerged, so the watchdog that would have caught this
independently was never installed.

No new root cause, no state transition (severity still yellow, not escalated
to red), no code changed on this branch. Not re-notifying the user this round
since check five already escalated this and nothing material has changed
beyond elapsed time — will re-escalate immediately if severity moves to red or
if the pattern breaks (cycle resumes, or stays frozen past ~96h/4 days).

## Update 2026-08-07T04:10 UTC — seventh check, ~72.5h and still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **72.5h** (was 64.5h at the last check — the gap grew by
almost exactly one oversight interval, the seventh in a row). New observation
worth recording: the `"currently-running phase is 172.3h old"` figure embedded
in the severity line has been **the literal string `172.3h` in every single one
of these seven checks**, spanning from the very first fetch (2026-08-05) through
today — it has never moved, even as `now - last_cycle` climbed from ~9h to
~72.5h over the same period. That is strong confirmation the whole gist payload
is a one-time frozen render (this number was computed once, at push time, and
baked into the static text) rather than being recalculated on each fetch — not
just the trading fields (`last_cycle`, `pending_prs`, `host commit`) but the
severity/reasons line itself.

`pending_prs` still stops at 8; PR #9 and #11 remain unlisted despite merging
2026-08-04. No new root cause, no state transition (severity still yellow),
no code changed on this branch. `now - last_cycle` is still under the ~96h/4-day
re-escalation threshold set at the fifth check, so not re-notifying the user
this round — will re-escalate on a move to red, or once elapsed time crosses
that threshold.

## Update 2026-08-07T12:30 UTC — eighth check, ~80.9h and still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **80.9h** (was 72.5h at the last check — the gap grew by
almost exactly one oversight interval, the eighth in a row). No new root
cause, no state transition (severity still yellow, not red), no code changed
on this branch. Still under the ~96h/4-day re-escalation threshold set at the
fifth check, so not re-notifying the user this round — will re-escalate on a
move to red, or once elapsed time crosses that threshold (projected next
check, ~88.9h, would still be under it; the check after, ~96.9h, would cross
it if the pattern holds).

## Update 2026-08-07T20:09 UTC — ninth check, ~88.5h and still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **88.5h** (was 80.9h at the last check — the gap grew by
almost exactly one oversight interval, the ninth in a row). `pending_prs`
still stops at 8; #9 and #11 remain unlisted despite merging 2026-08-04. No
new root cause, no state transition (severity still yellow, not red), no code
changed on this branch.

Still under the ~96h/4-day re-escalation threshold set at the fifth check, so
not re-notifying the user this round — but the *next* scheduled check
(~96.5h) is projected to cross it for the first time. Absent a change before
then, the following check should escalate on elapsed time alone even without
a severity transition.

## Update 2026-08-08T04:10 UTC — tenth check, ~96.5h, crosses the 96h threshold

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **96.5h** (was 88.5h at the prior check — the tenth
consecutive identical interval jump, exactly 4.0 calendar days of zero
recorded cycles). This crosses the ~96h/4-day re-escalation threshold set at
the fifth check, as projected. No new root cause beyond what's documented
here (host asleep vs. cron/launchd unloaded vs. broken gist delivery still
cannot be distinguished without hands-on access), no state transition
(severity still yellow per the frozen payload, not red). Re-notified the user
per the plan set at check five (recorded in the PR body; this doc file was
not amended in that same pass — reconciled here).

## Update 2026-08-08T12:18 UTC — eleventh check, ~104.7h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **104.7h** (was 96.5h at the prior check — the eleventh
consecutive identical interval jump, ~4.4 calendar days of zero recorded
cycles). `pending_prs` still stops at 8; #9 and #11 remain unlisted despite
merging 2026-08-04, consistent with zero fresh cycles run since. No new root
cause, no state transition (severity still yellow, not red), no code changed
on this branch.

Already re-escalated to the user at the prior check (96h threshold). No new
threshold was set for what comes after 96h, so using judgment here: not
re-notifying again this round since nothing has changed since that
notification 8h ago — would be a duplicate ping with no new information.
Setting the next re-escalation trigger explicitly: a move to severity=red, OR
`now - last_cycle` crossing **168h (7 calendar days)**, OR any change at all
in `last_cycle`/`host commit`/`pending_prs` (which would mean the host came
back). Absent one of those, future checks should keep updating this doc and
the PR body silently.

## Update 2026-08-08T20:06 UTC — twelfth check, ~112.5h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **112.5h** (was 104.7h at the prior check — the twelfth
consecutive identical interval jump, ~4.7 calendar days of zero recorded
cycles). No new root cause, no state transition (severity still yellow, not
red), no code changed on this branch. Per the trigger set at check eleven —
severity=red, staleness crossing 168h, or any change in
`last_cycle`/`host commit`/`pending_prs` — none has fired, so not
re-notifying the user this round; updating this doc and the PR body silently
as planned.

## Update 2026-08-09T04:09 UTC — thirteenth check, ~120.5h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **120.5h** (was 112.5h at the prior check — the
thirteenth consecutive identical interval jump, exactly 5.0 calendar days of
zero recorded cycles). No new root cause, no state transition (severity
still yellow, not red), no code changed on this branch. Per the trigger set
at check eleven — severity=red, staleness crossing 168h, or any change in
`last_cycle`/`host commit`/`pending_prs` — none has fired (120.5h is still
under the 168h/7-day threshold), so not re-notifying the user this round;
updating this doc and the PR body silently as planned. Absent a change, the
16th check (~152.5h) is still projected to land under 168h; the 17th
(~160.5h) or 18th (~168.5h) is where the threshold should first cross.

## Update 2026-08-09T12:14 UTC — fourteenth check, ~128.6h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **128.6h** (was 120.5h at the prior check — the
fourteenth consecutive identical interval jump, ~5.36 calendar days of zero
recorded cycles). No new root cause, no state transition (severity still
yellow, not red), no code changed on this branch. Per the trigger set at
check eleven — severity=red, staleness crossing 168h, or any change in
`last_cycle`/`host commit`/`pending_prs` — none has fired (128.6h is still
under the 168h/7-day threshold), so not re-notifying the user this round;
updating this doc and the PR body silently as planned. Absent a change, the
18th or 19th check (~160.6h-168.6h) is where the threshold should first
cross.

## Update 2026-08-09T20:08 UTC — fifteenth check, ~136.5h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **136.5h** (was 128.6h at the prior check — the
fifteenth consecutive identical interval jump, ~5.69 calendar days of zero
recorded cycles). No new root cause, no state transition (severity still
yellow, not red), no code changed on this branch. Per the trigger set at
check eleven — severity=red, staleness crossing 168h, or any change in
`last_cycle`/`host commit`/`pending_prs` — none has fired (136.5h is still
under the 168h/7-day threshold), so not re-notifying the user this round;
updating this doc and the PR body silently as planned. Absent a change, the
18th or 19th check (~160.5h-168.5h) is where the threshold should first
cross.

## Update 2026-08-10T04:08 UTC — sixteenth check, ~144.5h, still frozen

Re-fetched the gist again. Still **byte-identical** to every prior check:

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

`now - last_cycle` ≈ **144.5h** (was 136.5h at the prior check — the
sixteenth consecutive identical interval jump, exactly 6.0 calendar days of
zero recorded cycles). No new root cause, no state transition (severity
still yellow, not red), no code changed on this branch. Per the trigger set
at check eleven — severity=red, staleness crossing 168h, or any change in
`last_cycle`/`host commit`/`pending_prs` — none has fired (144.5h is still
under the 168h/7-day threshold), so not re-notifying the user this round;
updating this doc and the PR body silently as planned. Absent a change, the
next check (~152.5h) is still projected to land under 168h; the one after
(~160.5h) or the one after that (~168.5h) is where the threshold should
first cross.
