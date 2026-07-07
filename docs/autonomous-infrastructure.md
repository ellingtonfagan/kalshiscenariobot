# Autonomous Infrastructure Plan

Cursor, Codex, or any other IDE can edit this project, but autonomy must live in the
repo as runnable phases, scheduler jobs, persisted state, and audited risk gates.

## Current Autonomous Phases

- `ksobot status`: reports mode, live blockers, kill switch state, latest risk snapshot,
  order counts, and key artifacts.
- `ksobot portfolio-sync`: mirrors Kalshi balance and positions into local artifacts and
  the risk snapshot table.
- `ksobot source-check`: reports whether structured odds, fallback score/news, and
  opinion feeds are configured. Provider probes are opt-in with
  `NBABOT_SOURCE_CHECK_NETWORK=1`.
- `ksobot slate-discovery`: discovers candidate events/lines from SportsGameOdds,
  The Odds API, ESPN fallback context, and mapped Kalshi markets.
- `ksobot slate-verify`: rejects social-only, ESPN-only, duplicate, or unmapped slate
  candidates before research.
- `ksobot market-matcher`: refreshes slate/book handoffs, matches open Kalshi markets to
  comparable structured odds candidates, records order-book deltas, and writes
  `market_matches.json` plus `execution_slate.json`.
- `ksobot candidate-ranker`: resolves exact market identity, de-vigs sportsbook odds into
  consensus model probability, compares that to executable Kalshi price, and writes
  `candidate_ranker.json` plus `edge_candidates.json`.
- `ksobot research-agent`: writes full evidence to `research_bundle.json` and a narrowed
  `market_candidates.json` handoff containing only `trade_eligible` rows.
- `ksobot daily-cycle`: runs the autonomous control loop for one configured event:
  slate discovery, verification, portfolio sync, market discovery, quote snapshot,
  order book watch, market matching, edge ranking, research, explicit-mode execution,
  backtest if a learning log exists, then status.

`daily-cycle` is the activation chain. A scheduler only has to wake that one phase; each
successful agent records which next agent it activated:

```text
source-check -> slate-discovery -> slate-verify -> portfolio-sync -> discover-markets
  -> snapshot-market -> book-watch -> market-matcher -> candidate-ranker
  -> research-agent -> execution gate -> backtest -> status
```

`market-matcher` writes `market_matches.json` and `execution_slate.json`.
`execution_slate.json` is an order-book-aware review list, not an order instruction.

`candidate-ranker` writes `candidate_ranker.json` and `edge_candidates.json`.
`edge_candidates.json` is an edge-model output, not an order instruction. For Kalshi
multivariate markets, the ranker evaluates component legs from `mve_selected_legs` and
only treats the composite as exact when every supported leg is exactly matched.

`research-agent` writes `research_bundle.json` and `market_candidates.json`.
`market_candidates.json` is the fast handoff for downstream agents, but it is not an
order instruction. The execution phase still builds intents from fresh snapshots and
passes them through `risk.py`.

Slate verification, market matching, research, and execution refresh stale handoff
artifacts before consuming them. The shared freshness budget is
`NBABOT_STALE_MARKET_SECONDS`.

`daily-cycle` does not paper trade by default. It skips execution unless
`NBABOT_EXECUTION_MODE` is `live` or `demo`. Live execution still requires:

```bash
NBABOT_EXECUTION_MODE=live
NBABOT_DRY_RUN=0
NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY
```

The kill switch, stale data check, liquidity/spread check, exposure caps, daily-loss cap,
SGP-adjusted probability requirement, and audit log still apply.

## Recommended 24-Hour Loop

Run this single entry point from cron, launchd, or systemd:

```bash
*/10 * * * * /Users/ellingtonfagan/Downloads/nba-scenario-bot/.venv/bin/ksobot daily-cycle
```

For live trading, use a server or always-on machine with:

- `.env` configured.
- Private Kalshi key stored outside git.
- `data/KILL_SWITCH` available as the emergency stop.
- `NBABOT_MAX_DAILY_LOSS_UNITS` and `NBABOT_MAX_GAME_EXPOSURE_UNITS` set conservatively.
- Telegram or webhook alerts enabled.

## Next Agents To Build

1. `candidate-ranker`: learn from enough settled samples to rank candidate types across
   sports.
2. `book-risk`: expand depth, VWAP, fillable size, and slippage checks into a dedicated
   risk artifact.
3. `portfolio-sync` expansion: sync open orders, fills, fees, and realized P&L.
4. `settlement-audit`: verify final settlement and detect void/rule mismatches.
5. `calibration-agent`: summarize backtest failures and update conservative overrides.

## Role Boundaries

- **Scheduler:** decides when to run.
- **Activation chain:** records which agent activated the next agent and why a step was
  skipped or failed.
- **Research agents:** produce candidates and evidence.
- **Risk gate:** decides whether a candidate may be sent to execution.
- **Execution:** submits orders only after live gates and risk approval.
- **Telegram/webhook:** reports status and can later issue narrow commands.
- **Codex/Cursor:** development tools, not runtime dependencies.

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
