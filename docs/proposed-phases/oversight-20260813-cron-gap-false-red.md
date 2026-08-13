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
