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
- `ksobot daily-cycle`: runs the autonomous control loop for one configured event:
  portfolio sync, market discovery, quote snapshot, order book watch, explicit-mode
  execution, backtest if a learning log exists, then status.

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

Run this from cron, launchd, or systemd:

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

1. `slate-discovery`: choose today's candidate events across sports using
   `config/sources.yaml`.
2. `research-agent`: generate structured, source-backed candidate theses using the
   hierarchy in `docs/source-plan.md`.
3. `candidate-ranker`: convert theses into normalized market candidates.
4. `book-risk`: require fresh depth, VWAP, fillable size, and slippage checks.
5. `portfolio-sync` expansion: sync open orders, fills, fees, and realized P&L.
6. `settlement-audit`: verify final settlement and detect void/rule mismatches.
7. `calibration-agent`: summarize backtest failures and update conservative overrides.

## Role Boundaries

- **Scheduler:** decides when to run.
- **Research agents:** produce candidates and evidence.
- **Risk gate:** decides whether a candidate may be sent to execution.
- **Execution:** submits orders only after live gates and risk approval.
- **Telegram/webhook:** reports status and can later issue narrow commands.
- **Codex/Cursor:** development tools, not runtime dependencies.

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
