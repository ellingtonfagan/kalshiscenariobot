# Oversight 2026-08-10/11: root cause found for stale "currently-running phase" alarm; host is back; new qual win-rate reason

**Update (2026-08-16, 8-hourly check).** Fix still not landed; alarm still
climbing at wall-clock rate as predicted. `main` tip is unchanged since the
last check — still commit `424f5d609a2e960367bd14fccde9540ef84bb6cc`
(verified: `git log -1 origin/main` matches the gist's `host commit`
exactly). Re-verified directly against `origin/main:src/nbabot/monitor.py`:
`_cycle_summary()` (line 137) still builds `open_starts` at lines 154-163
with no "abandoned once a later cycle finishes cleanly" check, and
`oldest_open` (line 164) is still an unbounded `min()` with no expiry —
byte-identical logic to the 2026-08-13 and 2026-08-11 diagnoses, same line
numbers. `evaluate_severity()` (line 663-664) still fires yellow once
`currently_running_hours > 6.0` with no ceiling. The proposed one-function
fix below is unchanged and still applies directly to `main`.

Alarm trajectory: 172.3h (check 18) -> 333.7h (check 19, 2026-08-11) ->
389.7h (check 20, 2026-08-13) -> **475.2h** (this check, 2026-08-16) — still
climbing at ~wall-clock rate (85.5h elapsed since the last check, alarm grew
85.5h), exactly as this doc's fix would predict for an orphaned ledger row
that never expires.

Current gist snapshot (fetched 2026-08-16):

```
severity: yellow reasons=currently-running phase is 475.2h old
alive: True last_success_hours=4.0762
last_cycle: 2026-08-16T11:16:41.027021-04:00 edges=0 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=424f5d609a2e960367bd14fccde9540ef84bb6cc
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
health_alert: False delivery.failing=False
trades_today: count=0
pending_prs: 6,8,13,15,16
```

Only the known, already-diagnosed `currently-running phase` alarm is firing
this round — no other reasons present. Every other health signal (`alive`,
`last_success_hours=4.08`, `health_alert=False`, `delivery.failing=False`,
exposure clean/not diverged, no hard_error) confirms the bot itself is
healthy right now. Nothing here is new; per this doc's own re-notification
trigger this does not warrant a fresh investigation — same unfixed bug, one
more data point confirming the "climbs at wall-clock rate" prediction.

Two new PRs appeared in `pending_prs` since the last check (15, 16) — both
are unrelated oversight/fix PRs already tracked separately, not new reasons
for this alarm.

---

**Update (2026-08-13, 8-hourly check).** Fix still not landed. `main` moved
(commit `424f5d6`, merge of PR #14 `phase-26-monitor-on-main`) and
`src/nbabot/monitor.py` now lives on `main` itself (it didn't before — this
doc's original diagnosis cited it on `phase-25-oversight`), but PR #14 did
**not** include the one-function `open_starts` guard proposed below. Verified
directly against `origin/main:src/nbabot/monitor.py`: `_cycle_summary()`
(now at line 137) still builds `open_starts` at lines 154-165 with no
"abandoned if a later cycle finished" check, and `oldest_open` (line 164)
is still an unbounded min with no expiry — identical logic to what was
diagnosed on 2026-08-11, just renumbered. This is the predicted outcome
called out in that update's "re-notification trigger" note ("re-escalate
... if the orphan-ledger fix lands and `currently-running phase` still
doesn't clear — would mean this diagnosis was wrong"); here the fix simply
hasn't landed at all, so the alarm keeps climbing at wall-clock rate as
predicted: 172.3h (check eighteen) -> 333.7h (check nineteen, 2026-08-11)
-> **389.7h** (this check, 2026-08-13).

Current gist snapshot (fetched 2026-08-13):

```
severity: yellow reasons=currently-running phase is 389.7h old
alive: True last_success_hours=4.2106
last_cycle: 2026-08-12T21:41:20 edges=0 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=424f5d609a2e960367bd14fccde9540ef84bb6cc
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
health_alert: False delivery.failing=False
trades_today: count=0
pending_prs: 6,8,13
```

Both other reasons from the 2026-08-11 update (`0 orders despite trade-eligible
edges`, `qual win rate drop`) are **gone this round** — only the known,
already-diagnosed `currently-running phase` alarm remains, and every other
health signal (`alive`, `last_success_hours=4.21`, `health_alert=False`,
`delivery.failing=False`, exposure clean/not diverged) confirms the bot
itself is healthy. Nothing here is new or distinct from the existing
diagnosis, so per this doc's own re-notification trigger this does not
warrant a fresh investigation — it's the same unfixed bug, now with two
more data points confirming the "climbs at wall-clock rate" prediction.
The proposed fix below (`src/nbabot/monitor.py`, now at line 137/154-165 on
`main` instead of `phase-25-oversight`) is unchanged and still applies
directly to `main`.

---

**Update to this PR (nineteenth check, 2026-08-11).** Previous checks in this
doc/#13 and predecessor #12 tracked 18 consecutive snapshots of a frozen
`last_cycle` / stale host commit. That condition **cleared this round** — see
"What changed" below. This update replaces the investigation-only status with
a concrete root cause and a scoped fix for the one alarm that is still firing
(`currently-running phase`), plus honest non-diagnosis of the two other
reasons.

## What changed since the last check

Fields the re-notification trigger (carried from #12/#13) explicitly watches
for — `last_cycle`, `host commit`, `pending_prs` — **all three changed**:

| field | check eighteen (PR #13, 2026-08-10T20:08 UTC) | this check (2026-08-11) |
|---|---|---|
| `last_cycle` | `2026-08-03T23:37:22` (frozen) | `2026-08-10T16:12:09` (fresh, 1.65h old) |
| `alive` / `last_success_hours` | not reported as alive | `True`, `1.6486` |
| host commit | `0d9ae5d3...` (stale `phase-25-oversight` tip) | `67509a59...` |
| `pending_prs` | `3,4,5,6,7,8` | `6,8,13` |

`67509a59038bbef1279173f96132ec727d3230d4` is exactly `origin/main`'s current
tip (`git rev-parse origin/main` in this checkout matches). The host is now
running current `main`, cycles are running again, and PRs 3/4/5/7 have merged
since check eighteen. **The multi-day stall #12/#13 tracked is resolved.**

## What's real (current gist, fetched 2026-08-11)

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges;
  qual win rate dropped 50.0% vs 7-day baseline; currently-running phase is 333.7h old
last_cycle: 2026-08-10T16:12:09.937861-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=67509a59038bbef1279173f96132ec727d3230d4
exposure: authoritative_game=0.0u authoritative_portfolio=0.0u diverged=False
qual: placed=37 filled=17 settled=17 W-L=7-10 win_rate=0.4118 pnl=$73.1039
pnl: week=$-59.529 concentration=True
```

## Diagnosis, per reason

### 1. `currently-running phase is 333.7h old` — root-caused, fix proposed

This metric is **not** "a phase is hung right now" — it is the age of the
**oldest unmatched `"started"` row** in the per-run ledger
`data/scheduled_cycle_runs.jsonl` (never committed; per-install state).

Writer, `src/nbabot/cycle_health.py` (branch `origin/phase-25-oversight`,
PR #8 — this file does not exist on `main` yet):
- `record_cycle_started()` (line 55) appends `{"event": "started", "started_at": <ts>}`.
- `record_cycle_finished()` (line 68) requires the caller to pass back that
  same `started_at` string and appends `{"event": "finished", "started_at": <ts>, ...}`.

Reader/aggregator, `src/nbabot/monitor.py` (same branch), `_cycle_summary()`:
- Line 152-153: `finished_starts` = set of `started_at` values that appear on
  any `"finished"` row.
- Line 160-162: any `"started"` row whose `started_at` is **not** in that set
  goes into `open_starts` — permanently, with no age cutoff.
- Line 163: `oldest_open = min(open_starts, ...)` — the **oldest** unmatched
  start, not the most recent.
- Line 175-177: `currently_running_hours = now - oldest_open`.
- `evaluate_severity()` line 600-601 fires yellow once this exceeds 6h.

**The bug:** if a cycle process is killed before it can call
`record_cycle_finished` (exactly what a multi-day host stall / sleep / crash
does — the same event #12 first documented on 2026-08-05), its `"started"`
row is orphaned forever. `oldest_open` will keep returning that one ancient
orphan even after the host recovers and every subsequent cycle starts and
finishes cleanly, because nothing ever expires or reconciles an unmatched
start. `currently_running_hours` will climb without bound
(172.3h at check eighteen -> 333.7h now, growing at wall-clock rate, not
cycle rate) regardless of present-day health. This explains why the alarm
kept firing/growing even as `last_cycle`, `alive`, and `last_success_hours`
all show the bot is healthy right now.

**Proposed fix** (branch `phase-25-oversight` / PR #8, where these files
live — not `main`):

In `_cycle_summary()`, an unmatched `"started"` row should only count as
*currently* running if no cycle has finished since it began. Once any
`"finished"` row exists with `finished_at` later than a given orphan's
`started_at`, that orphan is abandoned, not in-flight — exclude it from
`open_starts`:

```python
# src/nbabot/monitor.py, inside _cycle_summary(), replacing the
# open_starts loop (~line 155-162)
last_finished_at = _parse_dt(finishes[-1].get("finished_at")) if finishes else None
open_starts: list[tuple[datetime, dict[str, Any]]] = []
for row in starts:
    started = _parse_dt(row.get("started_at"))
    if not started or str(row.get("started_at")) in finished_starts:
        continue
    if last_finished_at is not None and last_finished_at > started:
        continue  # a later cycle finished cleanly -> this start was abandoned, not running
    open_starts.append((started, row))
```

Small, single-function change. No touch to `cycle_health.py`'s ledger schema
or writer, no touch to live-gate code, no touch to `risk.py`/`sizing.py`.

### 2. `last cycle placed 0 orders and had trade-eligible edges` — not diagnosable from repo+gist alone

`edges=2 orders=0` this cycle. `exposure.diverged=False` and
`authoritative_game=0.0u` (well under the 20u effective cap), so this is
**not** the phantom-exposure pattern from Round 2 / PR #9 — that path is
clear. Beyond that, the actual block reason lives in each intent's
`RiskDecision.reasons` (`src/nbabot/risk.py:evaluate_trade_intent`,
`~line 140-415` — many independent checks: freshness, spread, plausible-edge,
disagreement veto, near-miss/QAQ gating, daily caps), written into
`data/demo_execute.json` on the host but **not surfaced in the gist**. This
exact pattern (edges found, 0 orders placed) has recurred across many
historical rounds in `docs/edge-engine-progress.md` (e.g. Round 20) and was
sometimes correct/expected risk-gate behavior, not a bug. Guessing which of
the ~10 checks in `risk.py` blocked these 2 specific intents without reading
`demo_execute.json` on the host would be fabrication. **Investigation-only,
not a fix**: next Codex session should read the host's
`data/demo_execute.json` (or equivalent `receipts[].decision.checks`) for
this cycle and report which named check(s) failed for both edges before any
code changes are proposed here.

### 3. `qual win rate dropped 50.0% vs 7-day baseline` — new reason, not diagnosable from repo+gist alone

Current qual win rate is `0.4118` (7-10) but qual `pnl=$73.1039` is still the
single largest positive contributor to `all_time=$69.6339` — consistent with
the pre-existing `concentration=True` flag (one or two large winners covering
many small losses), not necessarily a new defect. The monitor's own
auto-recommendation already flags this: *"Qual engine performance degrading;
check retrieval / groundedness"* (`src/nbabot/monitor.py:build_recommendations`,
branch `phase-25-oversight`). No file:line root cause is claimable from the
gist's aggregate win-rate delta alone — would need per-signal groundedness
scores and retrieved-evidence quality from the host's `research.sqlite`
(`qual_signals`, `qual_postmortems` tables per `qual_rag.py`/`qual_learning.py`).
**Investigation-only.**

## Tests to add (for the one proposed fix, reason 1)

A unit test in whichever test module covers `_cycle_summary`/`evaluate_severity`
on `phase-25-oversight` (currently `tests/test_smoke.py`, ledger fixtures at
lines ~7420/7516/7583 per that branch): write a ledger with (a) one orphaned
`"started"` row from N hours ago, (b) a later `"started"`/`"finished"` pair
that completed cleanly after it. Assert `currently_running_hours` is now
`None` (or based on the still-open real cycle, if any) instead of `N` hours —
this is the exact defect that produced the false 333.7h reading tonight and
would have caught it before it shipped.

## Verification checklist

- `.venv/bin/pytest -q` on `phase-25-oversight` — should stay at the branch's
  existing pass count (no unrelated regressions).
- New test above passes; a targeted `-k` run showing before/after (revert the
  one-line guard, confirm the old test fails) would be strong evidence.
- `git diff --stat` for the reason-1 fix should show exactly
  `src/nbabot/monitor.py | ~8 ++++` and one new/edited test file — no other
  files.
- Do NOT attempt a code fix for reasons 2 or 3 in this pass; those are
  investigation asks only per above.

## Guardrails

No commit/push beyond what's scoped above. No live env vars set. No live
orders. Do not edit live gates (`src/nbabot/agents/live_execute.py`, live
gate env vars, `risk.py` thresholds, `sizing.py`, `MIN_EDGE` values).

## Re-notification trigger (carried forward)

The host-recovery trigger fired on 2026-08-11 (all three watched fields
changed) and is reflected in that update; already reported to the user. As
of 2026-08-16 the alarm has now been open, root-caused, and unfixed for six
consecutive 8-hourly checks (2026-08-10 through 2026-08-16). Next:
re-escalate again only if severity flips to **red**, a *new* distinct reason
appears (i.e. not the `currently-running phase` alarm), or the orphan-ledger
fix lands and `currently-running phase` still doesn't clear (would mean this
diagnosis was wrong).
