# AGENTS.md — instructions for Codex (and any coding agent)

This repo is a **single-game NBA scenario-parlay monitor for Kalshi**. It encodes
game-script "scenarios", polls live prices + box score, detects which script is
becoming true, and logs outcomes to recalibrate its own probability priors over time.

You (the agent) are continuing this project. Read this whole file before editing.

---

## 0. The non-negotiable honesty contract (DO NOT WEAKEN)

This bot **reports**, it does not chase. Same-game parlay legs are **correlated**, so
naive multiplied payouts are wrong. Every output that mentions a bet MUST:

1. Show the **SGP-adjusted** joint probability / payout, never a naive product.
2. Cap any suggested stake at **≤5 units**, and **only** increase stake after a loss if there is viable risk or an edge that you run by a human first.
3. **Refuse** to "find" a target payout by stacking longshots.
4. Flag risk-5 scenarios as **hope bets** explicitly.
5. Append the guardrail footer from `src/nbabot/guardrails.py` (`GUARDRAIL_FOOTER`).

These live in `guardrails.py` and are asserted in `tests/test_smoke.py`. **If you remove
or soften them, the tests must fail.** Do not delete the assertions to make tests pass.

Default run mode is **monitor-only** (`NBABOT_DRY_RUN=1`). Live order placement exists
only because the human explicitly requested it. It must remain gated behind
`NBABOT_EXECUTION_MODE=live`, `NBABOT_DRY_RUN=0`, and
`NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY`, and every order must pass `risk.py`.
The research override may waive only the minimum-edge check. It requires the exact
environment acknowledgment, a named human approver, an evidence rationale, at least
two sources, and a stake of at most 1 unit. It must never bypass any other risk check,
and every approved use must be written to the audit log.

---

## 1. What each piece is

```
src/nbabot/
  config.py        Load .env + config/<GAME_ID>.{game,scenarios}.yaml
  kalshi.py        Signed Kalshi REST client (RSA-PSS). Prices + positions + balance.
  scores.py        ESPN box score + win-probability. Live game state per player.
  news.py          Lineups / inactives interface (stub — wire a real source).
  scenarios.py     Scenario model + the live state engine (ON_TRACK/DRIFTING/AT_RISK/DEAD).
  triggers.py      §5 live triggers (Towns foul math, pace, Wemby passivity, …).
  calibration.py   Brier score, sgp_haircut, the JSONL learning log.
  guardrails.py    §7 standing orders + footer. DO NOT WEAKEN.
  research.py      SQLite mirror for snapshots, backtests, audit, risk, orders.
  audit.py         Append-only audit.jsonl + dlq.jsonl.
  risk.py          Pre-execution risk gate. Must reject unsafe paper/demo trades.
  sizing.py        Capped Kelly helpers; still capped by the 5-unit rule.
  execution.py     Paper/demo/live execution records. Live orders require explicit gates.
  backtesting.py   Local scenario replay metrics from the learning log.
  marketdata.py    Scenario-leg quote snapshot helpers.
  ui.py            Dependency-free local dashboard served by `nbabot ui`.
  alerts.py        Compact-block formatter + delivery (stdout or webhook).
  agents/
    base.py        load_context(game_id) → Context shared by all agents.
    baseline.py    T-4h: pull prices, set entry_implied_p, flag market-vs-prior edges.
    lineups.py     T-90m: confirm starters/inactives, void scenarios on key scratch.
    lock.py        T-30m: re-pull, freeze the live board.
    heartbeat.py   tip→buzzer: ONE live tick. Emits only on change. Detects final.
    reconcile.py   T+30m: resolve legs to 1/0, append log, recompute calibration.
    backtest.py     No-network local replay from data/<GAME_ID>.log.jsonl.
    snapshot_market.py Capture mapped Kalshi quote snapshots.
    paper.py        Local paper fills only after risk gate approval.
    demo_execute.py Kalshi demo only after risk gate approval.
    ui.py           Serve the local browser UI.
cli.py             `nbabot <phase>` dispatch.
config/            Per-game YAML (game snapshot + scenario library + market_map).
data/              Runtime artifacts: board snapshots, hb state, log.jsonl (gitignored).
scheduler/         Portable crontab + the original OpenClaw cron file (reference).
```

The original game agents map 1:1 to the live-game phases. `cli.py phase=live` is an
alias for one `heartbeat` tick so a plain crontab can drive the live loop. The research,
paper/demo execution, and UI agents are explicit opt-in phases.

## 2. How to run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                      # or: pip install -r requirements.txt
cp .env.example .env                  # fill in the NEW Kalshi key id
# drop the RSA private key at secrets/kalshi-private-key.pem

nbabot baseline       # T-4h
nbabot lineups        # T-90m
nbabot lock           # T-30m
nbabot heartbeat      # one live tick (loop this every ~10m during the game)
nbabot reconcile      # after the buzzer
nbabot backtest       # no-network local replay
nbabot snapshot-market # capture mapped Kalshi quote snapshots
nbabot paper          # local paper fills only
nbabot demo-execute   # Kalshi demo only; requires NBABOT_EXECUTION_MODE=demo
nbabot live-execute   # real-money Kalshi; requires live gates + risk approval
nbabot ui             # local dashboard on 127.0.0.1:8765

pytest                # smoke tests (no network; everything is mockable)
```

Every phase also runs as `python -m nbabot <phase>`.

## 3. Data contracts (keep these stable — agents depend on them)

- **`kalshi.KalshiClient.prop_prices(game_tag)`** → `dict[(player, stat, line)] -> Quote`
  where `Quote` has `.bid .ask .mid .ticker`. `player` is lowercase surname, `stat` is
  one of `points|rebounds|assists|threes|minutes`, `line` is an int.
- **`scores.get_game_state(event_id)`** → `GameState` with `.period .clock .state`
  (`pre|in|post`), `.status_detail`, `.home_wp`, and `.players[name] -> PlayerLine`
  (`min pts reb ast threes fouls`). Names are full display names.
- **`scenarios.evaluate(scenario, prices, game_state)`** → `ScenarioState` with `.state`,
  `.legs_live` (per-leg implied + on/off track), `.live_payout_x` (haircut-applied).
- **Learning log** (`data/<GAME_ID>.log.jsonl`): one JSON object per reconcile, shape in
  `calibration.LogEntry`. Append-only. `calibration.recompute()` reads the whole file.

If you change a contract, update every caller AND `tests/test_smoke.py` in the same change.

## 4. Where to extend (good first tasks for Codex)

- `news.py` is a stub: wire a real inactives/lineups source (official injury feed or a
  licensed sports API). Return `Inactives(out=[...], questionable=[...])`. **Do not scrape
  paywalled/ToS-restricted feeds.**
- `scenarios.py` market_map covers player props + game winner. `game_total` and
  `spurs_cover` are marked `resolvable=False` (no clean single Kalshi market wired) — add
  the correct series tickers and flip them on.
- `scores.find_event()` resolves the ESPN event id by matchup keyword; if ESPN changes
  shape, fix the parser there (single choke-point).
- `alerts.deliver()` supports stdout + generic webhook POST. Add Slack/Telegram block
  formatting if the human wants richer alerts.

## 5. Conventions

- Pure-stdlib + the 3 deps in `pyproject.toml`. No heavy frameworks.
- Network calls only in `kalshi.py`, `scores.py`, `news.py`, `alerts.py`. Everything else
  is pure and unit-testable.
- Fail soft on live data: a missing player or a reshaped ESPN payload must downgrade a
  scenario to `AT_RISK`/`void` with a logged reason, never crash the heartbeat.
- Times are `America/New_York`. Money is integer cents internally; format to dollars only
  at the edge.
- Keep alerts to the compact block in `alerts.format_block`. No walls of text.

## 6. Porting notes (this came from a Claude Code plugin)

The signing logic and ESPN win-prob math were ported from a working Kalshi plugin
(`cle_watcher.py` / `live_signals.py`). Behavior preserved:
- RSA-PSS, SHA-256, digest-length salt; timestamp in ms; sign `ts+METHOD+path`.
- Implied prob = `yes_price_cents / 100`. DraftKings "Live" line is de-vigged for a fair prob.
The old `monitor-cron.jsonc` (OpenClaw scheduler) is kept under `scheduler/` for reference;
the portable `scheduler/crontab.txt` is the one that runs anywhere.
