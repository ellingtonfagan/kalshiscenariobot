# Codex prompt: surface why ranker "edges" don't become trade-eligible candidates

## 1. What's real

8-hourly oversight check (2026-08-19) fetched
`https://gist.githubusercontent.com/ellingtonfagan/01680ea910e51feb0bb95183f841efcb/raw`:

```
# monitor NBA-2026-FINALS-G3
- severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges
## alive
- alive: True last_success_hours=3.7516
- last_cycle: 2026-08-18T18:58:44.830050-04:00 edges=4 orders=0 hard_error=None
- host: Ellingtons-MacBook-Pro-4.local commit=40b1b5374d79aca6254ec7565c611d523a2b2152
## trading
- exposure: authoritative_game=0.0u authoritative_portfolio=0.0u ... diverged=False
- trades_today: count=0 stake=$0 tickers=none
- live_gates: {'execution_mode_live': False, 'dry_run_disabled': False, 'live_trading_ack': False, 'kill_switch_clear': True}
## health
- health_alert: False reasons=none
- escalated: last cycle placed 0 orders and had trade-eligible edges
- errors: none
## recommendations
- Cycles are failing at high rate; check hard_error in monitor snapshot
```

`edges=4 orders=0 hard_error=None`. No caps look exhausted (`diverged=False`, `count=0` trades
today, `kill_switch_clear=True`), so this isn't a risk-gate block. The standing recommendation
("check hard_error") is a dead end: `hard_error=None`.

**This same yellow condition fired 2026-08-18 with `edges=1 orders=0`** (see closed PR #19,
`docs/proposed-phases/oversight-20260818-blocked-reason-not-surfaced.md`, merged into `main` as
a doc only — its proposed code change was never implemented; the gist still has no `blocked=`
field today). That prompt diagnosed one *specific* cause (a demo-exchange rejection swallowed by
`227cfcb`) and proposed plumbing `blocked_reason` from `scheduled_demo_cycle.json` through to the
gist. This prompt goes one level deeper: even with that plumbing in place, `blocked_reason` would
only ever read `"no-candidates"` in this failure mode (see below) — a string that still doesn't
explain *why* candidate-ranker's 4 edge-passing rows never became trade-eligible candidates. This
prompt supersedes #19: it implements the same ledger→monitor→gist plumbing *plus* the missing
diagnostic payload, in one self-contained change.

## 2. Suspected root cause(s)

`edges=4` in the gist comes from `candidate_ranker.json`'s **`edge_pass_count`**
(`src/nbabot/agents/scheduled_demo_cycle.py:307` — `"edges_found": _count(ranker,
"edge_pass_count")`), which is candidate-ranker's own final `passes_edge` flag
(`src/nbabot/candidate_ranker.py:841`) — i.e. "raw/net edge cleared the configured minimum,
after fees, confluence veto, and composite-market blocking."

But **order placement never reads `edge_pass_count` or candidate-ranker's `passes_edge` directly.**
`_candidate_intents()` (`src/nbabot/agents/paper.py:400-437`) filters on `row.get("trade_eligible")`
from `research_bundle.json`'s `market_candidates`, which is a **separate, stricter** flag computed
in `src/nbabot/slate.py::research_bundle()` — re-deriving its own edge from `market_snapshot.json`
and adding blockers candidate-ranker's `edge_pass_count` never checks:

- `src/nbabot/slate.py:1211-1237` (single configured-game candidates): `trade_eligible = not
  blockers`, where `blockers` can include (independently of edge quality):
  - `src/nbabot/slate.py:1217-1218` — **`if not verified_ids: blockers.append("slate verifier
    has no approved research candidates")`. This one applies unconditionally to every row** — if
    `slate-verify` approved zero candidates that cycle, `trade_eligible` is `False` for all of
    them regardless of edge, and `edge_pass_count` (candidate-ranker) and `trade_eligible_count`
    (research-agent) diverge to 4-vs-0.
  - `src/nbabot/slate.py:1225-1227` — a second, independently-computed edge check
    (`edge_fields["edge"]` from `market_snapshot.json`, not candidate-ranker's net-of-fees edge).
  - `src/nbabot/slate.py:1229` — missing `sgp_adjusted_prob`.
  - `src/nbabot/slate.py:1231` — spread missing or wider than `max_spread_cents`.
  - `src/nbabot/slate.py:1233,1236` — missing order-book verification / zero fillable contracts.
- `src/nbabot/slate.py:1306-1324` (broad-slate candidates): a parallel, similarly-independent
  blocker list gated additionally on `paper_demo_mode`.

Any one of these (most plausibly the `verified_ids` catch-all, or a stale/thin order book) can
take candidate-ranker's `edge_pass_count=4` straight to `trade_eligible_count=0`, which is exactly
what `_candidate_intents` sees, and `demo_execute.run()` (`src/nbabot/agents/demo_execute.py:35-41`)
returns `{"reason": "no-candidates", ...}` before ever calling `execute_intent_batch`. In
`scheduled_demo_cycle.py::_demo_order_summary` (`src/nbabot/agents/scheduled_demo_cycle.py:82-84`),
that becomes `blocked_reason = "no-candidates"` — a string that, even after #19's plumbing lands,
would tell an external oversight check nothing about *which* of the 5+ independent blockers fired.

This is not a risk/sizing/live-gate bug — it's a **visibility gap**: `research_bundle()` already
computes a per-row `blockers` list (`src/nbabot/slate.py:1275` / `:1373`) that would answer this
immediately, but it's never aggregated or surfaced past `research_bundle.json`.

## 3. Proposed fix

Small, additive, no behavior/threshold change. Four files, in order:

1. **`src/nbabot/slate.py`, end of `research_bundle()`** (near `:1394`, alongside
   `"trade_eligible_count"`): add an aggregated breakdown of blockers for rows that cleared their
   own edge check but are still ineligible, e.g.:
   ```python
   "edge_ok_but_blocked_breakdown": _blocker_breakdown(market_candidates),
   ```
   where `_blocker_breakdown` counts, per distinct blocker string, how many rows have
   `row["blockers"]` non-empty AND do not include an edge-related blocker (i.e. rows whose edge
   was fine but something else blocked them) — a small pure function, no new I/O, same shape as
   the existing `sum(1 for row in ...)` reducers already in this function.

2. **`src/nbabot/agents/scheduled_demo_cycle.py`, `_run_cycle_body`** (near `:293,307-319`): when
   `blocked_reason == "no-candidates"`, append the top 1-2 entries of
   `research.get("edge_ok_but_blocked_breakdown")` (the `research` step result already in scope
   at `:290`) to `blocked_reason`, e.g.
   `"no-candidates (top blocker: slate verifier has no approved research candidates x4)"`.
   Cap the appended text (e.g. 200 chars) — same reasoning as #19's truncation note.

3. **`src/nbabot/cycle_health.py::record_cycle_finished`** (`:68-86`): add
   `"blocked_reason": payload.get("blocked_reason")` to the persisted row (this is #19's proposed
   change — implement it here since #19 was never actually coded, only drafted).

4. **`src/nbabot/monitor.py`**: add `last_cycle_blocked_reason` to `_cycle_summary` (near `:175`,
   reading `last.get("blocked_reason")`), to the `MonitorSnapshot` dataclass (near `:35`) and its
   construction (near `:771`), and append it to the `last_cycle:` markdown line (`:850`) only when
   non-null — same shape #19 specified. Do **not** touch `evaluate_severity()` (`:627-660`); this
   is visibility only, not a new gate.

## 4. Tests to add

- `tests/test_smoke.py`: a `research_bundle`/slate test asserting that when one candidate row has
  a passing edge but `verified_ids` is empty, `trade_eligible` is `False` and
  `edge_ok_but_blocked_breakdown["slate verifier has no approved research candidates"] == 1`
  (build on the existing `research_bundle` fixtures already in the file rather than new ones).
- `tests/test_smoke.py`: a `scheduled_demo_cycle` test where `demo-execute` returns
  `{"reason": "no-candidates"}` and the `research-agent` step result carries a non-empty
  `edge_ok_but_blocked_breakdown` — assert the resulting `blocked_reason` string includes the top
  blocker text.
- `tests/test_smoke.py`: extend the existing `cycle_health`/`monitor` tests (see #19's notes,
  around the fixtures near line 8067+) to assert `blocked_reason` round-trips from
  `record_cycle_finished` into `MonitorSnapshot.last_cycle_blocked_reason` and into the rendered
  markdown, and is absent/`None` for old ledger rows that predate this field (backward compat).

## 5. Verification checklist

```
.venv/bin/pytest -q                                    # expect: all passing, same count + N new
.venv/bin/pytest -q tests/test_smoke.py -k "research_bundle or cycle_health or monitor or scheduled_demo_cycle"
git diff --stat                                         # expect only: slate.py,
                                                          #   scheduled_demo_cycle.py,
                                                          #   cycle_health.py, monitor.py,
                                                          #   tests/test_smoke.py — no changes to
                                                          #   execution.py, risk.py, sizing.py,
                                                          #   live_execute.py, candidate_ranker.py
```

Then a real (non-live) invocation to confirm the field flows end to end:

```
NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot scheduled-demo-cycle
NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot monitor
tail -1 data/scheduled_cycle_runs.jsonl   # confirm "blocked_reason" key present
cat data/monitor.md                        # confirm last_cycle line shows blocked=... when set
cat data/research_bundle.json | python3 -c "import json,sys; print(json.load(sys.stdin).get('edge_ok_but_blocked_breakdown'))"
```

Look for a non-empty breakdown whenever `edges_found > 0` and `demo_orders_placed == 0` in the
same run — that's the case this fix exists to explain.

## 6. Guardrails

No commit/push. No live env vars set. No live orders. Do not edit live gates.
