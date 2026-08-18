# Codex prompt: surface demo-execute blocked_reason in the cycle ledger and monitor gist

## 1. What's real

8-hourly oversight check (2026-08-18) fetched `https://gist.githubusercontent.com/ellingtonfagan/01680ea910e51feb0bb95183f841efcb/raw`:

```
# monitor NBA-2026-FINALS-G3
- severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges
## alive
- alive: True last_success_hours=1.9093
- last_cycle: 2026-08-17T20:26:15.441902-04:00 edges=1 orders=0 hard_error=None
- host: Ellingtons-MacBook-Pro-4.local commit=227cfcb98e41fd56013802fa1db1b6264abd3fa9
## health
- health_alert: False reasons=none
- escalated: last cycle placed 0 orders and had trade-eligible edges
- errors: none
## recommendations
- Cycles are failing at high rate; check hard_error in monitor snapshot
```

`edges=1 orders=0 hard_error=None` — one trade-eligible edge, zero demo orders placed, no hard
error recorded. The recommendation ("check hard_error") is a dead end here: `hard_error=None`,
so there is nothing to check. Balance/exposure/trades_today all show `$0` stake, `0` positions,
`0` trades today, consistent with the order not landing.

The commit the gist reports (`227cfcb`) is `Handle demo exchange rejections without failing
cycles (#18)`, merged 2026-08-17 20:24:20 ET — **two minutes before** this cycle ran at 20:26:15.

## 2. Suspected root cause(s)

`227cfcb` changed `execute_demo()` so that a definitive demo-exchange HTTP 4xx response no longer
raises (which previously would have shown up as a hard error) — it's now converted into an
audited `"rejected"` receipt and the cycle continues cleanly:

- `src/nbabot/execution.py:311-336` — `execute_demo()` catches the exception, calls
  `_http_rejection_details(e)`, and on a definitive 4xx returns
  `OrderReceipt(..., "rejected", rejection)` instead of raising.
- `src/nbabot/agents/scheduled_demo_cycle.py:88-107` (`_demo_order_summary`) only counts
  `status in {"submitted", "filled"}` toward `placed`; a `"rejected"` receipt falls into the
  `blocked` list and produces a human-readable `blocked_reason` string
  (`src/nbabot/agents/scheduled_demo_cycle.py:293,319`).
- That `blocked_reason` is written into `scheduled_demo_cycle.json` and delivered in the
  Telegram report body (`_format_report`, line 245: `f"blocked={blocked}"`) — but it is **not**
  passed into `record_cycle_finished()`:
  - `src/nbabot/cycle_health.py:75-86` only persists `exit_code`, `candidates`, `edges`,
    `orders_placed`, `hard_error` into `scheduled_cycle_runs.jsonl`. `blocked_reason` is dropped.
- The monitor then reads that ledger (`src/nbabot/monitor.py:137-177`, `_cycle_summary`) and
  exposes only `last_cycle_edges_found` / `last_cycle_orders_placed` / `last_cycle_hard_error`
  into the gist snapshot (`src/nbabot/monitor.py:850`). There is no field for *why* the edge
  didn't turn into an order.

Net effect: as soon as a demo order gets exchange-rejected (insufficient demo balance, bad
ticker, price outside the exchange's band, market closed, etc.) — exactly the case `227cfcb` was
built to make non-fatal — the monitor's only visible signal is the generic
`eligible_edges_no_orders` yellow (`src/nbabot/monitor.py:659-660`), and remote/oversight checks
that only have gist + repo access (no SSH, no local `data/` dir) cannot tell that from a stale
research/market-snapshot key mismatch (`_candidate_intents`, `src/nbabot/agents/paper.py:414-437`),
a `mode-blocked` return, or a `no-candidates` return. All four collapse to the same
`edges=1 orders=0 hard_error=None` line.

This prompt does not attempt to fix *why* the single order didn't place (unknown — could be a
legitimate exchange rejection, now correctly handled as non-fatal by `227cfcb`). It fixes the
observability gap that made this cycle's `227cfcb`-shipped behavior indistinguishable, from the
outside, from every other zero-orders cause.

## 3. Proposed fix

Small, additive, no behavior change to trading/risk/execution:

1. In `src/nbabot/agents/scheduled_demo_cycle.py`, `_run_cycle_body` already computes
   `blocked_reason` (line 293) and puts it in `payload` (line 319). No change needed there.
2. In `src/nbabot/cycle_health.py::record_cycle_finished` (line 75-86), add one field to the
   appended row:
   ```python
   "blocked_reason": payload.get("blocked_reason"),
   ```
3. In `src/nbabot/monitor.py::_cycle_summary` (around line 175-177), read it back:
   ```python
   "last_cycle_blocked_reason": last.get("blocked_reason"),
   ```
   and add the matching field to the `MonitorSnapshot` dataclass (near line 34-37) and its
   construction (near line 770-773).
4. In `src/nbabot/monitor.py::_render_markdown` (or wherever the `"- last_cycle: ..."` line is
   built, line 850), append the blocked reason when present, e.g.:
   ```python
   f"- last_cycle: {s['last_cycle_run_time_et']} edges={s['last_cycle_edges_found']} "
   f"orders={s['last_cycle_orders_placed']} hard_error={s['last_cycle_hard_error']}"
   + (f" blocked={s['last_cycle_blocked_reason']}" if s.get('last_cycle_blocked_reason') else "")
   ```
   (Match existing string-building/quoting style in that function; don't reformat the whole
   function.)
5. Do **not** change `evaluate_severity()`'s yellow/red logic — this is purely additive
   visibility, not a new gate.

Truncate/sanitize `blocked_reason` before persisting if it can contain raw exchange response
bodies (check what `_demo_order_summary` actually puts in `blocked` — currently just the
rejection `reasons` list or a status string, so it should already be short; add a length cap
(e.g. 500 chars) only if you find it isn't).

## 4. Tests to add

- `tests/test_smoke.py`: extend or add near the existing `test_...blocked_reason...` cases
  around line 7341 — assert that when `scheduled_demo_cycle.run()` produces a non-empty
  `blocked_reason` (e.g. a `demo-execute` result whose order receipt is `status="rejected"`
  with `response={"reasons": ["insufficient balance"]}`, matching `227cfcb`'s new rejection
  shape), the row appended to `scheduled_cycle_runs.jsonl` via `record_cycle_finished` contains
  `blocked_reason == "insufficient balance"` (or whatever `_demo_order_summary` derives).
- `tests/test_smoke.py`: a `monitor` test (see existing tests around line 8067-8213 that build
  fake `scheduled_cycle_runs.jsonl` rows) asserting `evaluate_severity`/snapshot building still
  produces `eligible_edges_no_orders` yellow for `edges=1, orders_placed=0`, and that
  `MonitorSnapshot.last_cycle_blocked_reason` (or the rendered markdown) now carries the reason
  through when the ledger row has `blocked_reason` set, and is `None`/absent when it doesn't
  (backward compat with old ledger rows written before this change).

## 5. Verification checklist

```
.venv/bin/pytest -q                                   # expect: all passing, same count + N new
.venv/bin/pytest -q tests/test_smoke.py -k "cycle_health or monitor"
git diff --stat                                        # expect: cycle_health.py, monitor.py,
                                                         #   tests/test_smoke.py only — no
                                                         #   changes to execution.py, risk.py,
                                                         #   sizing.py, live_execute.py
```

Then a real (non-live) invocation to confirm the new field actually flows end to end:

```
NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot scheduled-demo-cycle   # or a phase that exercises
                                                                    # record_cycle_finished with
                                                                    # a non-empty blocked_reason
NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot monitor
tail -1 data/scheduled_cycle_runs.jsonl   # confirm "blocked_reason" key present in the row
cat data/monitor.md                        # confirm the last_cycle line shows blocked=... when set
```

Look for: the `finished` row in `scheduled_cycle_runs.jsonl` gaining a `blocked_reason` key, and
`data/monitor.md` / `monitor_snapshot.json`'s last_cycle line including it whenever non-null.
`git diff --stat` should show only the three files above touched, with a small (double-digit)
line delta — this is an additive plumbing change, not a rewrite.

## 6. Guardrails

No commit/push. No live env vars set. No live orders. Do not edit live gates.
