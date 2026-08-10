# Codex prompt: stale "currently-running phase" never clears

## 1. What's real

Gist snapshot (`monitor NBA-2026-FINALS-G3`), fetched 2026-08-04:

```
- severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
## alive
- alive: True last_success_hours=0.7993
- last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
## health
- escalated: last cycle placed 0 orders and had trade-eligible edges, currently-running phase is 172.3h old
- errors: none
```

Two distinct yellow reasons are packed into one string. This prompt is scoped to
the second one only: **`codex_phase_stalled_gt_6h`** — "currently-running phase
is 172.3h old" (~7.2 days). The bot itself is alive and healthy
(`last_success_hours=0.7993`, `errors=none`) — this is a monitoring-ledger
integrity bug, not a live outage.

(The first reason, phantom exposure causing 0 orders despite trade-eligible
edges, already has an open fix: PR #9 "Phase 23d: phantom exposure + demo_execute
batch + rejection reasons". Do not duplicate that work here.)

## 2. Suspected root cause

`codex_phase_stalled_gt_6h` fires from `currently_running_hours`, computed in
`_cycle_summary()` (monitor.py, on the `phase-25-oversight` branch not yet on
this trunk) by scanning `scheduled_cycle_runs.jsonl` for `"started"` rows with
no matching `"started_at"` among `"finished"` rows, then taking the **oldest**
such orphaned start:

```python
oldest_open = min(open_starts, key=lambda item: item[0])[0] if open_starts else None
...
"currently_running_hours": (
    round((now - oldest_open).total_seconds() / 3600.0, 4)
    if oldest_open is not None else None
),
```

The ledger writes come from `src/nbabot/agents/scheduled_demo_cycle.py:244-352`:

```python
def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    started_at = record_cycle_started(ctx)          # cycle_health.py:55 — writes "started"
    ...
    (five try/except blocks around individual pipeline steps; each catches
     Exception and appends to hard_errors — so those don't lose the row)
    ...
    ctx.write_json("scheduled_demo_cycle.json", payload)
    payload["report_delivery_ok"] = deliver(...)
    payload["delivery_failures"] = record_delivery_result(...)
    ctx.write_json("scheduled_demo_cycle.json", payload)
    record_cycle_finished(ctx, started_at=started_at, payload=payload)  # line 352 — writes "finished"
    return payload
```

`record_cycle_finished` is called exactly once, at the very end, **not** inside
a `try`/`finally`. Anything that raises outside the five guarded blocks —
`load_context()`, `ctx.write_json`, `deliver()`, `record_delivery_result()`, an
OS-level crash (see PR #5's own description: "cycle crashed with `OSError:
[Errno 24] Too many open files]` ... before writing the summary or placing
orders") — leaves a `"started"` row with no corresponding `"finished"` row,
permanently.

Because `_cycle_summary()` takes the **oldest** unfinished start with no
expiry, one crash from days ago keeps inflating `currently_running_hours`
forever, even though every cycle since has finished cleanly
(`last_success_hours=0.7993` proves the bot is fine right now). The alert
cannot self-clear — it needs either a code fix or a manual ledger edit.

## 3. Proposed fix

Small, scoped, no live-gate or sizing/risk code touched:

1. **`src/nbabot/agents/scheduled_demo_cycle.py`** — wrap the body of `run()`
   from just after `started_at = record_cycle_started(ctx)` through the return
   in `try`/`finally`, calling `record_cycle_finished(ctx, started_at=started_at,
   payload=payload)` in the `finally`. Build a minimal `payload` (e.g.
   `{"exit_code": 1, "hard_error": str(exc)}`) if the crash happens before
   `payload` is otherwise constructed, so an uncaught exception still closes
   out the ledger row instead of losing it.
2. **`src/nbabot/cycle_health.py`** (optional, only if (1) alone doesn't fully
   address stale historical rows) — when computing `open_starts` in
   `_cycle_summary`, drop rows older than some sane bound (e.g. 24h) from the
   "currently running" calculation and instead surface them as a distinct
   `abandoned_cycle_count` / `oldest_abandoned_hours` signal, so a single
   ancient orphan doesn't dominate the "currently running" reading forever.
   Only do this if it doesn't change red/yellow thresholds — that's
   monitor.py's `evaluate_severity`, which is explicitly out of scope (don't
   touch threshold values).
3. Do **not** touch `evaluate_severity`'s threshold constants, live_execute.py,
   any live-gate env var, risk.py, or sizing.py.

## 4. Tests to add

- `tests/test_smoke.py` (or wherever `scheduled_demo_cycle` is tested): a test
  that monkeypatches one of the five guarded steps — or, better, something
  after them (e.g. `ctx.write_json` or `deliver`) — to raise, then asserts
  `record_cycle_finished` (or the ledger file) still received a `"finished"`
  row for the matching `started_at`. This is the test that would have caught
  the defect: today an exception there propagates out of `run()` with the
  `"started"` row never closed.
- A `cycle_health.py` test asserting a `"started"` row from >24h ago with no
  matching finish doesn't dominate `currently_running_hours` if you implement
  fix (2).

## 5. Verification checklist

```
.venv/bin/pytest -q                                   # confirm no regressions; same 5 pre-existing
                                                        # fixture failures as before, no new failures
.venv/bin/pytest -q -k "scheduled_demo_cycle or cycle_health"
git diff --stat                                        # expect ~2 files touched:
                                                        #   scheduled_demo_cycle.py, cycle_health.py
                                                        # (+ their test files). No changes to
                                                        # live_execute.py, risk.py, sizing.py.
NBABOT_EXECUTION_MODE=demo NBABOT_DRY_RUN=1 .venv/bin/ksobot scheduled-demo-cycle
                                                        # exit 0; scheduled_cycle_runs.jsonl gets
                                                        # a "finished" row matching the "started" row
                                                        # even if you force one of the five guarded
                                                        # steps to raise for a manual smoke test
```

Look for: no new `"started"`-without-`"finished"` rows appended to
`data/scheduled_cycle_runs.jsonl` after a forced failure; `git diff --stat`
shows no touches to `live_execute.py`, live gate env vars, `risk.py`
thresholds, or `sizing.py`.

## 6. Guardrails

No commit/push. No live env vars set. No live orders. Do not edit live gates.
