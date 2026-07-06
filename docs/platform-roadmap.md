# Kalshi Sports Orderbook Engine Roadmap

Working name: **Kalshi Sports Orderbook Engine**.

This repo started as an NBA scenario monitor. The durable core is broader:
discover Kalshi sports markets, watch order books, compare executable prices to a
sport-specific model, gate risk, execute only when explicitly enabled, and learn from
settled outcomes.

## Rename Strategy

Do not rename the Python import package in one large change. Keep `nbabot` as the
compatibility package while adding the broader `ksobot` command alias.

Recommended migration order:

1. Rebrand README, docs, package metadata, and UI copy. **Done.**
2. Add `ksobot` as the preferred CLI while keeping `nbabot`. **Done.**
3. Move NBA-specific code behind adapter boundaries. **Started:** runtime phases load
   `ctx.adapter`, and NBA scores/scenarios/triggers/market parsing live under
   `src/nbabot/adapters/nba/` with compatibility wrappers at the old import paths.
4. After adapters are stable, optionally migrate package imports from `nbabot` to a
   broader package name with a compatibility shim.

## Stable Core

These modules should become sport-agnostic:

- `kalshi.py`: signed REST client, market discovery, order books, portfolio/order state.
- `orderbook.py`: YES/NO ladder normalization, derived asks, VWAP, depth, slippage.
- `adapters/base.py`: sport adapter contract used by runtime phases.
- `agents/book_watch.py`: order book capture from generic candidate artifacts.
- `market_matcher.py`: Kalshi-first order-book deltas and execution-review slate.
- `research.py`: SQLite mirror for snapshots, orders, fills, risk, calibration.
- `risk.py`: execution gate, kill switch, exposure, liquidity, staleness, approvals.
- `execution.py`: paper/demo/live order request and audit recording.
- `alerts.py`: delivery and compact reporting.
- `ui.py`: local operational dashboard.

## Sport Adapter Contract

Every sport adapter should define:

- Event discovery source and event ID format.
- Market families and Kalshi series/ticker patterns.
- Lineup/injury/status source that is licensed or explicitly permitted.
- Live state parser and final-state resolver.
- Scenario schema and leg resolver.
- Calibration family keys.
- Order book/liquidity requirements.
- Backtest replay source.

Adapters should not place orders. They produce normalized scenario state and candidate
market mappings. Core risk/execution decides whether an order can be submitted.

## Candidate Ports

### Soccer

Current status: research-only artifacts already exist under `config/WC-*` and `docs/WC-*`.

Good first executable scope:

- Moneyline / draw-no-bet / team total / total goals.
- Scoreline-set scenarios using Poisson expected goals.
- Corners/cards only after a reliable market mapping and stat source exists.

Key blockers:

- Non-participation and void rules differ by market.
- Draws create three-way outcome logic.
- Live xG and lineup sources need licensing or explicit permission.

### MLB

Good first executable scope:

- Moneyline, run line, game total.
- Pitcher strikeouts, batter hits, home runs only after reliable player-market mapping.

Key blockers:

- Starting pitcher scratches materially change fair value.
- Weather, park, bullpen availability, and lineup changes matter.
- Extra innings alter total/run-line behavior.

### NFL

Good first executable scope:

- Moneyline, spread, total, selected player props.
- Drive/state-aware live trading only after robust game-state ingestion.

Key blockers:

- Injury/news latency is high-impact.
- Garbage-time and clock state are central to prop modeling.
- Player prop markets are sparse and may move violently after injuries.

### NHL

Good first executable scope:

- Moneyline, puck line, total goals.
- Shots-on-goal props only after player ice-time and goalie confirmation are wired.

Key blockers:

- Starting goalie confirmation is critical.
- Empty-net states affect totals and puck lines.
- Low-scoring distributions make correlation assumptions sensitive.

### College Basketball

Good first executable scope:

- Moneyline, spread, total.
- Team tempo and foul-state scenarios.

Key blockers:

- Team identifiers and neutral-site games require careful mapping.
- Player prop coverage is inconsistent.
- Data quality varies by competition.

### Tennis

Good first executable scope:

- Match winner, set winner, total games.

Key blockers:

- Retirement/walkover settlement rules.
- Serve order and surface effects dominate live fair value.
- Player injury/news signals are difficult to verify.

## Expansion Modules

1. `portfolio-sync`: reconcile live/demo orders, fills, positions, fees, and P&L.
2. `book-watch`: graduate from REST snapshots to websocket deltas for selected tickers.
3. `book-risk`: require fresh depth, VWAP, slippage, and fillability before execution.
4. `slate-discovery`: discover all relevant events for a sport/date.
5. `adapter-backtest`: replay adapter-specific states and order books.
6. `edge-lab`: compare Kalshi executable prices to independent fair-value sources.
7. `settlement-audit`: resolve final outcomes and detect rule/void mismatches.

## Execution Policy

All ports inherit the same core policy:

- Use SGP-adjusted or explicitly single-market probability math.
- Reject target-payout stacking.
- Flag risk-5 scenarios as hope bets.
- Enforce configured stake and exposure caps.
- Never let research override bypass kill switch, stale data, liquidity, order book,
  exposure, or live-trading acknowledgment checks.
- Record every candidate, rejection, order, fill, and final result.

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
