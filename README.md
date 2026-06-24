# nba-scenario-bot

A self-improving, single-game NBA **scenario-parlay monitor** for Kalshi.

> **It reports which game-script is becoming true. It does not chase bets.** Same-game
> parlay legs are correlated, so it always shows the SGP-adjusted joint probability, caps
> stakes at 5 units, and requires explicit human approval plus a viable edge before any
> increase after a loss. See `guardrails.py`.

It runs as five phases around tip-off:

| Phase | When | What it does |
|-------|------|--------------|
| `baseline`  | T-4h  | Pull every leg's Kalshi price → set entry prob; flag market-vs-prior edges |
| `lineups`   | T-90m | Confirm starters/inactives; void scenarios if a key player is out |
| `lock`      | T-30m | Freeze the live board (entry price + prior per leg) |
| `heartbeat` | tip→buzzer | One live tick: box score + prices → update scenario states, fire triggers, alert **only on change**; detect final |
| `reconcile` | T+30m | Resolve every leg 1/0, append to the learning log, recompute Brier + correlation haircut |

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # paste your NEW Kalshi key id
# put the RSA private key at secrets/kalshi-private-key.pem

nbabot baseline
nbabot lineups
nbabot lock
nbabot heartbeat              # loop this every ~10 min during the game
nbabot reconcile
nbabot backtest               # local no-network replay from the learning log
nbabot autopilot              # safe repeated orchestration for cron/launchd
nbabot ui                     # local browser UI at http://127.0.0.1:8765
```

Drive the live loop from cron — see `scheduler/crontab.txt`.

## How it learns

`reconcile` appends one row per leg and per scenario to `data/<GAME_ID>.log.jsonl`, then:

1. **Brier score** per market family (`*_points`, `*_rebounds`, …) and per scenario.
2. **Bias check** — if a family's priors run > +0.08 high over ≥15 samples, shrink them
   toward the market price.
3. **Correlation memory** — realized joint-hit rate vs `prod(leg priors)` becomes the
   `sgp_haircut` applied to future payout estimates.

Over many games the priors in `config/*.scenarios.yaml` get more honest, and the payout
math stops overstating longshots.

## Research + Execution Framework

The bot also has a conservative research and paper/demo execution framework:

```bash
nbabot backtest          # score local scenario history
nbabot discover-markets  # catalog open Kalshi NBA markets for this game tag
nbabot snapshot-market   # capture Kalshi quote snapshots for mapped legs
nbabot paper             # create local paper fills for approved single-leg intents
nbabot demo-execute      # Kalshi demo only; requires NBABOT_EXECUTION_MODE=demo
nbabot live-execute      # real-money Kalshi order; requires all live gates below
nbabot autopilot         # repeated safe runner: discover/snapshot/paper/live/reconcile
nbabot ui                # local dashboard
```

Runtime data stays under `data/`: JSON artifacts remain append/read-friendly, while
`data/research.sqlite` mirrors backtests, market snapshots, risk decisions, audit events,
paper/demo orders, fills, and risk snapshots for analysis.

Safety gates are always enforced before paper/demo orders: SGP-adjusted scenario context,
stake `<= 5 units`, minimum edge, stale quote rejection, per-game exposure cap, daily-loss
cap, liquidity spread cap, risk-5 hope-bet flag, and `data/KILL_SWITCH`.

`autopilot` is designed for cron/launchd/systemd. It can be run repeatedly:

- pregame: discover markets, baseline once, lineup/lock near tip, snapshot, paper
- live: heartbeat, snapshot, paper
- final: reconcile once, then backtest

Autopilot uses `paper` by default. It calls `demo-execute` when
`NBABOT_EXECUTION_MODE=demo`; it calls `live-execute` when `NBABOT_EXECUTION_MODE=live`.
Live execution is real-money and is blocked unless all live gates are set:

```bash
NBABOT_EXECUTION_MODE=live
NBABOT_DRY_RUN=0
NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY
```

Live orders still pass the same risk gate: SGP-adjusted scenario probability must be
present, stake must be `<= 5 units`, game/daily exposure caps must pass, quote data must
be fresh, spread must be acceptable, risk-5 scenarios must be flagged as hope bets, and
`data/KILL_SWITCH` must not exist. Orders use Kalshi's V2
`/portfolio/events/orders` endpoint with `immediate_or_cancel` limit orders.

Useful environment knobs:

```bash
NBABOT_EXECUTION_MODE=paper|demo|live
NBABOT_LIVE_TRADING_ACK=
NBABOT_RESEARCH_OVERRIDE_ACK=
NBABOT_RESEARCH_OVERRIDE_MAX_UNITS=1
NBABOT_MAX_DAILY_LOSS_UNITS=2
NBABOT_MAX_GAME_EXPOSURE_UNITS=5
NBABOT_MIN_EDGE=0.05
NBABOT_STALE_MARKET_SECONDS=90
NBABOT_MAX_SPREAD_CENTS=10
NBABOT_KILL_SWITCH=data/KILL_SWITCH
NBABOT_CALIBRATION_OVERRIDES=data/calibration_overrides.json
NBABOT_UI_PORT=8765
```

### Research override

The minimum-edge check has a narrow, audit-friendly override for a human-approved
research thesis. It does not disable the risk gate. Set:

```bash
NBABOT_RESEARCH_OVERRIDE_ACK=RESEARCH_OVERRIDE_APPROVED
NBABOT_RESEARCH_OVERRIDE_MAX_UNITS=1
```

The individual market-snapshot row must also set `research_override: true`, include a
named `research_approved_by`, an evidence-based `research_override_reason` of at least
80 characters, at least two `research_sources`, and an override stake no greater than
1 unit. Kill switch, stale quote, spread, exposure, daily-loss, loss-chasing, ticker,
SGP probability, hope-bet, dry-run, and live-trading acknowledgment checks still apply.

`reconcile` writes conservative learned calibration overrides to
`NBABOT_CALIBRATION_OVERRIDES`. Loading the bot applies those overrides in memory only:
automatic learning can shrink overconfident priors and SGP haircuts, but it does not
rewrite scenario YAML or inflate a scenario's SGP probability above the configured value.

Production live order placement is available only through `live-execute` with the live
gates above.

## Configure a new game

Copy the two YAMLs and point `NBABOT_GAME_ID` at them:

```bash
cp config/NBA-2026-FINALS-G3.game.yaml      config/MY-GAME.game.yaml
cp config/NBA-2026-FINALS-G3.scenarios.yaml config/MY-GAME.scenarios.yaml
# edit kalshi_game_tag, espn matchup keywords, and the scenario legs/priors
NBABOT_GAME_ID=MY-GAME nbabot baseline
```

## For agents continuing this repo

Read **`AGENTS.md`** first. It has the data contracts, the honesty contract you must not
weaken, and the list of good extension points.

---
*Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).*
