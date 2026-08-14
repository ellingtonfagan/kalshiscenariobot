# Oversight 2026-08-13: red "no successful cycle in >8h" is a false alarm — the demo-cycle cron cadence has a built-in ~15.7h overnight gap the freshness check doesn't account for

## What's real

Gist fetched 2026-08-13 (~11:54 UTC / ~07:54 ET):

```
severity: red reasons=no successful cycle in >8h
alive: False last_success_hours=10.2131
last_cycle: 2026-08-12T21:41:20.642162-04:00 edges=0 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=424f5d609a2e960367bd14fccde9540ef84bb6cc
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
health_alert: False reasons=none
delivery: failing=False consecutive_failures=0 last_success=2026-08-13T11:03:35.114542+00:00
errors: none
trades_today: count=0
pending_prs: 6,8,13
```

Every other health signal is clean: `health_alert=False`, `delivery.failing=False`
with a fresh `last_success` timestamp (the monitor itself ran and delivered
successfully ~50min before this check), no hard error on the last cycle, no
exposure divergence. The *only* red reason is the 8h staleness check, and the
last recorded cycle itself succeeded cleanly (`hard_error=None`) — it's just
old.

Host commit `424f5d6` is on `origin/main` (verified:
`git merge-base --is-ancestor 424f5d609a2e960367bd14fccde9540ef84bb6cc origin/main`
→ yes), so this branch is cut from `main`, not the older
`codex/broad-slate-market-matcher` (which predates `monitor.py` entirely and
doesn't contain it).

## Suspected root cause(s)

Two compounding gaps, both file:line-traceable:

**1. The active cron schedule has a gap wider than the alarm threshold.**

`scheduler/combined-crontab.txt` (the file its own header calls "the ONE file
to install") fires the demo cycle (`scheduler/run-demo-cycle.sh` →
`ksobot scheduled-demo-cycle`) at exactly four fixed ET times/day: 11:15,
16:10, 18:40, 23:35. The gap from 23:35 to the next day's 11:15 is **15h40m**
— nearly double both:
- `src/nbabot/monitor.py:635-637` — red fires when
  `hours_since_last_successful_cycle > 8.0`
- `src/nbabot/monitor.py:166` — `bot_alive` is `hours <= 6.0`

News-watch (`scheduler/news-watch-crontab.txt`, every 5 min 10:00-23:55 ET)
can trigger extra off-schedule cycles, but only on breaking injury/lineup
news, debounced 45min/team — it's not a substitute for a guaranteed
sub-8h cadence, and it doesn't run at all 00:00-10:00 ET.

**2. Overnight activity that *does* run is invisible to the freshness ledger.**

`scheduler/overnight-crontab.txt` runs `ksobot daily-cycle` hourly, 00:00-08:00
ET (MLB-scoped, monitor-only, no orders — a real, working cron job). But
`src/nbabot/agents/daily_cycle.py::run()` never calls
`cycle_health.record_cycle_started` / `record_cycle_finished` — grep confirms
only `src/nbabot/agents/scheduled_demo_cycle.py` (lines 11, 252, 365) writes to
`data/scheduled_cycle_runs.jsonl`. `monitor.py::_cycle_summary()` (lines
137-180) computes `hours_since_last_successful_cycle` and `bot_alive` purely
from that file. So the 8 real overnight hours of `daily-cycle` activity never
reset the staleness clock — even though the bot was almost certainly awake
and working.

Put together: `last_cycle` at 21:41 ET Aug 12 was very likely a news-watch
trigger, not a fixed slot. From there to now (~07:54 ET Aug 13, before the
11:15 ET fixed slot has even run) is exactly the kind of gap the schedule
guarantees every night, made worse because the overnight `daily-cycle` runs
that *did* happen in between don't count. This alarm is structurally
guaranteed to fire red for several hours every single night regardless of
bot health, not just tonight.

## Proposed fix

Small, scoped, monitoring-only — no live-gate code touched:

1. In `src/nbabot/agents/daily_cycle.py::run()`, call
   `cycle_health.record_cycle_started(ctx, phase="daily-cycle")` at entry (near
   line 26, alongside the existing `ResearchStore`/`AuditTrail` setup) and
   `cycle_health.record_cycle_finished(ctx, started_at=started_at, payload=..., phase="daily-cycle")`
   right before `return payload` (near line 400), mirroring
   `scheduled_demo_cycle.py` lines 252/365. Use the same `exit_code`/`hard_error`
   semantics already computed for the `daily_cycle.json` payload (`failures`
   count around line 390 can map to `hard_error` when nonzero).
2. Do not change the 6h/8h thresholds themselves — they're reasonable *if* the
   ledger reflects all real cycle activity, which after (1) it will: the
   overnight hourly `daily-cycle` runs will keep resetting the clock through
   00:00-08:00 ET, and the remaining 08:00-11:15 ET gap (~3h15m) is well under
   8h.
3. Leave `scheduled_demo_cycle.py`, `monitor.py`'s threshold constants, and all
   crontab files untouched — this is purely "make the existing daily-cycle
   phase report into the ledger it should already be reporting into."

## Tests to add

A test in whatever suite covers `cycle_health.py` / `daily_cycle.py` (check
`tests/` for existing `scheduled_demo_cycle` ledger tests and mirror the
pattern) asserting: after `daily_cycle.run()` completes successfully,
`data/scheduled_cycle_runs.jsonl` contains a `finished` row with
`exit_code=0` and `hard_error=None`, and
`monitor._cycle_summary(data_dir, now)["hours_since_last_successful_cycle"]`
reflects that finish time (not just `scheduled-demo-cycle` finishes). This is
exactly the case that would have caught tonight's false red.

## Verification checklist

- `.venv/bin/pytest -q` — should still pass in full; new test above should be
  red before the fix, green after.
- Real invocation: `NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot daily-cycle`
  (or whatever mode the overnight cron actually uses) against a scratch
  `data/` dir, then confirm `scheduled_cycle_runs.jsonl` gained a
  `started`/`finished` pair with `phase=daily-cycle`.
- `git diff --stat` expectation: `src/nbabot/agents/daily_cycle.py` (a handful
  of added lines), `src/nbabot/cycle_health.py` only if `EXPECTED_PHASES`
  needs `"daily-cycle"` added (check whether anything actually enforces that
  set — `cycle_health.py:110` uses it for a completeness check; add the phase
  name there too if so), plus the new test file. No changes to `monitor.py`
  thresholds, no changes to any `scheduler/*` file, no changes to execution/
  risk/sizing code.

## Guardrails

No commit/push beyond this doc. No live env vars set. No live orders. Do not
edit live gates (`live_execute.py`, live gate env vars, `risk.py` thresholds,
`sizing.py`, `MIN_EDGE` values).

---

## Update 2026-08-14: this is no longer a pure false positive — the demo-cycle cron appears to have stopped running

Gist fetched 2026-08-14 (~00:16 UTC / ~20:16 ET Aug 13):

```
severity: red reasons=no successful cycle in >8h
alive: False last_success_hours=23.2784
last_cycle: 2026-08-12T21:41:20.642162-04:00 edges=0 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=424f5d609a2e960367bd14fccde9540ef84bb6cc
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
health_alert: False reasons=none
delivery: failing=False consecutive_failures=0 last_success=2026-08-14T00:06:25.021068+00:00
errors: none
trades_today: count=0 tickers=none
pnl: today=$0.0 week=$12.23 all_time=$81.8639
pending_prs: 6,8,13,15
```

`last_cycle` is byte-identical to the timestamp in both the original diagnosis
(10.21h) and the first update (14.22h) — it has not advanced across two more
8-hourly oversight checks. That by itself was already flagged as consistent
with the diagnosis. But the fixed cron schedule
(`scheduler/combined-crontab.txt`: 11:15, 16:10, 18:40, 23:35 ET) independently
contradicts the pure-false-positive read: between `last_cycle` (21:41 ET Aug 12)
and this snapshot (~20:16 ET Aug 13), **four** fixed demo-cycle slots should
have fired — 23:35 (Aug 12), 11:15, 16:10, 18:40 (Aug 13) — and each one calls
`scheduled_demo_cycle.py::run()`, which records to the ledger via
`record_cycle_started` at line 252 *before* doing any real work. That path does
not depend on the `daily_cycle.py` gap described above at all. Four consecutive
misses on a mechanism that's supposed to be cron-gap-immune is a different
failure mode: the demo-cycle process itself is not running or is dying before
line `scheduled_demo_cycle.py:252`, not merely under-crediting overnight work.

Everything else is still clean (`health_alert=False`, fresh `delivery.last_success`
~10min before this snapshot, `exposure.diverged=False`, no errors) — whatever
component runs the meta-monitor/delivery loop is alive. It's specifically the
four fixed `run-demo-cycle.sh` cron slots that appear to be silent.

This repo checkout cannot see the host's actual crontab install state, log
files under `logs/demo-cycle-*.log`, or whether `run-demo-cycle.sh` is
erroring before Python even starts (e.g. broken `.venv`, path change, macOS
sleep/cron scheduling issue) — none of that is visible from gist + git alone.
The original ledger-recording fix for `daily_cycle.py` is still valid and
worth landing on its own merits, but it will **not** explain or fix four
missed fixed-slot cycles.

### Revised proposed action — investigate before assuming the original fix is sufficient

1. Keep the original `daily_cycle.py` ledger-recording fix (still correct,
   still monitoring-only, described above) — land it.
2. Add an investigation step to the Codex prompt: on the host, check whether
   `scheduler/combined-crontab.txt` is actually installed
   (`crontab -l | grep run-demo-cycle`) and check
   `logs/demo-cycle-*.log` for the most recent four expected slots
   (23:35 Aug 12, 11:15/16:10/18:40 Aug 13 ET) — confirm whether the cron
   fired at all, and if it fired, where `run-demo-cycle.sh` /
   `scheduled_demo_cycle.py` failed before reaching line 252. This step is
   read-only (log/crontab inspection), no code changes, and must be done on
   the host directly — it cannot be verified from this checkout.
3. Do not change `monitor.py` thresholds or crontab files based on this
   update alone — the schedule itself isn't the problem this time; something
   is preventing the schedule from executing.

Same guardrails as above: no commit/push beyond this doc, no live env vars,
no live orders, no live-gate edits.

---

## Update 2026-08-14 ~14:41 UTC (later 8-hourly check) — new evidence narrows the failure to before the ledger write, timed to the Phase 26 merge

Gist fetched 2026-08-14 ~14:41 UTC:

```
severity: red reasons=no successful cycle in >8h
alive: False last_success_hours=34.1174
last_cycle: 2026-08-12T21:41:20.642162-04:00 edges=0 orders=0 hard_error=None
host: EllingtonsMBP4.lan commit=424f5d609a2e960367bd14fccde9540ef84bb6cc
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
health_alert: False reasons=none
delivery: failing=False consecutive_failures=0 last_success=2026-08-14T05:22:54.036928+00:00
errors: none
trades_today: count=0 tickers=none
pnl: today=$0.0 week=$12.23 all_time=$81.8639
pending_prs: 6,8,13,15
```

Same `last_cycle` timestamp as the original report and both prior updates —
now unchanged across four consecutive 8-hourly checks and 34.1 stale hours.
Two new findings this round, both from re-reading the current
`scheduled_demo_cycle.py` on `origin/main` rather than just the gist:

**1. The stall's start lines up almost exactly with the Phase 26 merge, not
with anything in `daily_cycle.py`.** `git log` on `main` shows
`last_cycle` (21:41:20 -04:00, Aug 12) landed **12 minutes before**
`424f5d6` (`Merge pull request #14 from
ellingtonfagan/phase-26-monitor-on-main`, committed 21:53:10 -04:00, Aug 12
— the same commit the gist's `host: commit=` field names as currently
running). That merge's own message says it bundles three pieces "because
they land together to get cycles completing again" — i.e. it was a fix
attempt for a then-existing stall, and cycles have not completed since it
landed. That commit is the prime suspect, not a coincidence of timing.

**2. The absence of a "currently-running phase" reason rules out a hang
inside the new pre-cycle code, and points further upstream than previously
thought.** `scheduled_demo_cycle.py::run()` calls
`record_cycle_started(ctx)` at line 252 — before `execution_mode` is even
set and before any Phase 26 code runs — then (new in the 424f5d6 merge) runs
`order_reconcile.run()` at line 267 inside a bare `try/except Exception`
before `daily_cycle.run()`. If that pre-cycle call ever hung (blocked, never
raised) instead of erroring, `scheduled_cycle_runs.jsonl` would carry a
`started` row with no matching `finished` row, and
`monitor.py:_cycle_summary()`'s `open_starts`/`oldest_open` logic (lines
154-178) would surface a growing `currently-running phase is Xh old` red/
yellow reason (exactly what PR #13 tracked for a different orphan). This
gist shows only `no successful cycle in >8h` — no currently-running-phase
reason at all, across four checks spanning 34+ hours. That means no new
`started` row has been written since 21:41 Aug 12: the process is not
reaching line 252, so it isn't hanging inside `order_reconcile.run()` (or
anywhere else in `scheduled_demo_cycle.run()`) — the failure is upstream of
that, in `run-demo-cycle.sh`, the `ksobot` CLI entrypoint, or the cron
mechanism itself failing to invoke it, on or after the 424f5d6 merge.

Everything else stays consistent with the prior updates: `health_alert=False`,
`delivery.failing=False` with a `last_success` timestamp from ~9h before this
fetch (so whatever runs `ksobot monitor` on its own 2h `launchd` interval is
still alive and importing the package fine — ruling out a package-wide
import error as the cause, since `monitor` and the new `meta_check`/
`telegram_bot` modules share the same `agents/__init__.py` import list added
in 424f5d6), `exposure.diverged=False`, no errors, and PnL/balance unchanged.
This is a real, sustained scheduling/process failure, not a monitor false
positive.

### Revised investigation step (host-side, read-only, run first)

None of this is confirmable from gist + git alone; the check that would
disambiguate needs the host directly. In priority order:

1. `crontab -l | grep run-demo-cycle` — confirm the four fixed slots
   (`scheduler/combined-crontab.txt`) are actually installed and unchanged.
   Note the crontab file's own header warns `crontab <file>` hangs on this
   Mac and it must be piped via stdin (`cat ... | crontab -`) — if anyone
   re-ran the wrong install form recently, that alone could explain missed
   installs.
2. `tail -n 100 logs/demo-cycle-*-2335.log logs/demo-cycle-*-1115.log
   logs/demo-cycle-*-1610.log logs/demo-cycle-*-1840.log` for the four
   slots since Aug 12 23:35 — did `run-demo-cycle.sh` even start (any
   output), and if so, where did it stop? A silent/empty log for all four
   points at cron not invoking the script at all (or invoking a stale/wrong
   path) rather than a Python-level failure.
3. If the logs show the script started: `ps aux | grep -i ksobot` /
   `ps aux | grep -i scheduled_demo_cycle` for any process still alive from
   one of those four slots — a hung foreground call inside
   `daily_cycle.run()` (called after the ledger write at line 252, so it
   would still show a `currently-running` reason once one *does* fire) or a
   zombie left over from before 424f5d6 holding a resource (FD, DB lock) the
   new invocations block on.
4. Confirm `grep started data/scheduled_cycle_runs.jsonl | tail -5` — this
   directly tests finding (2) above: if there is truly no `started` row after
   21:41 Aug 12, that's conclusive that the process isn't reaching line 252,
   and the fix belongs in `run-demo-cycle.sh` / cron / the CLI dispatch path,
   not inside `scheduled_demo_cycle.py`.

Once (1)-(4) identify where execution actually stops, land a scoped fix there
(and keep the already-proposed `daily_cycle.py` ledger-recording fix from the
original report above — still valid, still worth landing, but confirmed now
not to be this failure's cause on its own). Do not touch `monitor.py`
thresholds, `order_reconcile.py`, or any `scheduler/*` file based on
speculation — only after (1)-(4) show which layer is actually failing.

Same guardrails as above: no commit/push beyond this doc, no live env vars,
no live orders, no live-gate edits.
