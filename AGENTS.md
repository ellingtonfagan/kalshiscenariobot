# AGENTS.md — instructions for Codex (and any coding agent)

This repo is a **Kalshi sports order book and scenario engine**. It started as a
single-game NBA scenario-parlay monitor and now keeps the NBA runtime behind a sport
adapter so other sports can be added later. It encodes game-script "scenarios", polls
live prices + box score, detects which script is becoming true, and logs outcomes to
recalibrate its own probability priors over time.

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
only because the human explicitly requested it. It must remain gated behind ALL FOUR
of these acknowledgments: `NBABOT_EXECUTION_MODE=live`, `NBABOT_DRY_RUN=0`,
`NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY`, and — for broad-slate intents
(anything other than the single configured game) — `NBABOT_BROAD_SLATE_EXECUTION=
BROAD_SLATE_TRADES_REAL_MONEY`. `execution.py` enforces all four; the honesty
contract is that this list stays synchronized with the code, not that it stays
short. Every order must also pass `risk.py`.
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
  adapters/
    base.py        SportAdapter interface + MarketQuotes shared by runtime phases.
    nba/           NBA implementation: scores, scenarios, triggers, market parsing.
  scores.py        Compatibility wrapper for adapters/nba/scores.py.
  news.py          Lineups / inactives interface (stub — wire a real source).
  scenarios.py     Compatibility wrapper for adapters/nba/scenarios.py.
  triggers.py      Compatibility wrapper for adapters/nba/triggers.py.
  calibration.py   Brier score, sgp_haircut, the JSONL learning log.
  guardrails.py    §7 standing orders + footer. DO NOT WEAKEN.
  research.py      SQLite mirror for snapshots, backtests, audit, risk, orders.
  audit.py         Append-only audit.jsonl + dlq.jsonl.
  risk.py          Pre-execution risk gate. Must reject unsafe paper/demo trades.
  sizing.py        Capped Kelly helpers; still capped by the 5-unit rule.
  execution.py     Paper/demo/live execution records. Live orders require explicit gates.
  backtesting.py   Local scenario replay metrics from the learning log.
  marketdata.py    Compatibility wrapper for adapters/nba/marketdata.py.
  sports.py        Sport-port registry for active/research/planned adapters.
  slate.py         Slate discovery, verification, and research-candidate helpers.
  market_matcher.py Match Kalshi slate markets to books, external lines, and deltas.
  market_identity.py Normalize Kalshi/sportsbook rows to deterministic market identities.
  odds_math.py     Odds conversion, de-vigging, outlier reporting, consensus probability.
  edge_engine.py   Pure model-vs-executable-price edge checks.
  candidate_ranker.py Rank matched markets by de-vigged consensus edge.
  odds_refresh.py  Shared artifact freshness checks and live refresh helpers.
  sources/         Source registry + readiness checks for slate/research inputs.
  ui.py            Dependency-free local dashboard served by `nbabot ui`.
  alerts.py        Compact-block formatter + delivery (stdout or webhook).
  agents/
    base.py        load_context(game_id) → Context with ctx.adapter shared by agents.
    baseline.py    T-4h: pull prices, set entry_implied_p, flag market-vs-prior edges.
    lineups.py     T-90m: confirm starters/inactives, void scenarios on key scratch.
    lock.py        T-30m: re-pull, freeze the live board.
    heartbeat.py   tip→buzzer: ONE live tick. Emits only on change. Detects final.
    reconcile.py   T+30m: resolve legs to 1/0, append log, recompute calibration.
    backtest.py     No-network local replay from data/<GAME_ID>.log.jsonl.
    snapshot_market.py Capture mapped Kalshi quote snapshots.
    book_watch.py   Capture side-aware order book depth from generic candidate artifacts.
    market_matcher.py Build market_matches.json and execution_slate.json.
    candidate_ranker.py Build candidate_ranker.json and edge_candidates.json.
    news_ingest.py  Fetch configured team RSS/Atom research items into SQLite.
    qual_research.py Run the local Codex CLI for cited qual probabilities.
    status.py       Summarize mode, live blockers, artifacts, risk, and orders.
    portfolio_sync.py Mirror Kalshi balance + positions into local state.
    daily_cycle.py  Autonomous discover/snapshot/book/execution/backtest/status loop.
    source_check.py  Secret-safe readiness report for external source feeds.
    slate_discovery.py Find candidate events/lines from structured sports feeds.
    slate_verify.py  Verify slate identity/source coverage before research.
    research_agent.py Produce trade-eligible research handoff artifacts.
    paper.py        Local paper fills only after risk gate approval.
    demo_execute.py Kalshi demo only after risk gate approval.
    ports.py        Export active/research/planned sport adapter registry.
    ui.py           Serve the local browser UI.
cli.py             `ksobot <phase>` dispatch; `nbabot <phase>` remains compatible.
config/            Per-game YAML (game snapshot + scenario library + market_map).
data/              Runtime artifacts: board snapshots, hb state, log.jsonl (gitignored).
scheduler/         Portable crontab + the original OpenClaw cron file (reference).
```

The original game agents map 1:1 to the live-game phases. `cli.py phase=live` is an
alias for one `heartbeat` tick so a plain crontab can drive the live loop. The research,
paper/demo execution, and UI agents are explicit opt-in phases.
For autonomous operation, prefer `ksobot daily-cycle` under cron/launchd/systemd. It is
the agent activation chain: each phase records which prior phase activated it and which
next phase it makes eligible. It does not paper trade by default; live execution still
requires the explicit live gates and risk approval.
The current chain is `source-check -> news-ingest -> slate-discovery -> slate-verify ->
portfolio-sync -> discover-markets -> snapshot-market -> book-watch -> market-matcher
-> qual-research -> candidate-ranker -> research-agent -> execution gate -> backtest -> status`. Slate verification,
market matching, research, and execution refresh stale odds/order-book handoff artifacts
before consuming them. `NBABOT_STALE_MARKET_SECONDS` controls those freshness checks.

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
nbabot source-check  # verify external source readiness without exposing secrets
nbabot slate-discovery # discover candidate sports events/lines
nbabot slate-verify  # reject social/ESPN-only slate candidates
nbabot market-matcher # match open Kalshi slate, order-book deltas, and execution review rows
nbabot news-ingest  # fetch team-scoped RSS/Atom research items
nbabot qual-research # produce cited qualitative probabilities for unpriced markets
nbabot candidate-ranker # compute de-vigged consensus probability and edge
nbabot research-agent # produce research_bundle + trade-eligible market_candidates
nbabot paper          # local paper fills only
nbabot demo-execute   # Kalshi demo only; requires NBABOT_EXECUTION_MODE=demo
nbabot live-execute   # real-money Kalshi; requires live gates + risk approval
nbabot ui             # local dashboard on 127.0.0.1:8765

pytest                # smoke tests (no network; everything is mockable)
```

Every phase also runs as `python -m nbabot <phase>`. `ksobot` is the preferred CLI;
`nbabot` remains valid for existing scripts.

## 3. Data contracts (keep these stable — agents depend on them)

- **`kalshi.KalshiClient.prop_prices(game_tag)`** → `dict[(player, stat, line)] -> Quote`
  where `Quote` has `.bid .ask .mid .ticker`. `player` is lowercase surname, `stat` is
  one of `points|rebounds|assists|threes|minutes`, `line` is an int.
- **`scores.get_game_state(event_id)`** → `GameState` with `.period .clock .state`
  (`pre|in|post`), `.status_detail`, `.home_wp`, and `.players[name] -> PlayerLine`
  (`min pts reb ast threes fouls`). Names are full display names.
- **`scenarios.evaluate(scenario, prices, game_state)`** → `ScenarioState` with `.state`,
  `.legs_live` (per-leg implied + on/off track), `.live_payout_x` (haircut-applied).
- **`agents.base.Context.adapter`** → active `SportAdapter`. Runtime phases should call
  this adapter for event lookup, live state, market discovery, pricing, scenario
  evaluation, trigger evaluation, snapshots, and final resolution.
- **Learning log** (`data/<GAME_ID>.log.jsonl`): one JSON object per reconcile, shape in
  `calibration.LogEntry`. Append-only. `calibration.recompute()` reads the whole file.

If you change a contract, update every caller AND `tests/test_smoke.py` in the same change.

## 4. Where to extend (good first tasks for Codex)

- `news.py` is a stub: wire a real inactives/lineups source (official injury feed or a
  licensed sports API). Return `Inactives(out=[...], questionable=[...])`. **Do not scrape
  paywalled/ToS-restricted feeds.**
- `adapters/nba/scenarios.py` market_map covers player props + game winner. `game_total` and
  `spurs_cover` are marked `resolvable=False` (no clean single Kalshi market wired) — add
  the correct series tickers and flip them on.
- `adapters/nba/scores.find_event()` resolves the ESPN event id by matchup keyword; if ESPN changes
  shape, fix the parser there (single choke-point).
- New sports should implement `SportAdapter` first, then register in `adapters/__init__.py`
  and `sports.py`. Do not add new sport logic directly to `agents/`.
- `alerts.deliver()` supports stdout + generic webhook POST. Add Slack/Telegram block
  formatting if the human wants richer alerts.
- External slate/research sources are declared in `config/sources.yaml`. Structured odds
  feeds should drive discovery; ESPN hidden endpoints are fallback only; social/opinion
  feeds are context only and must never trigger execution by themselves.

## 5. Conventions

- Pure-stdlib + the 3 deps in `pyproject.toml`. No heavy frameworks.
- Network calls only in `kalshi.py`, sport-adapter live data modules such as
  `adapters/nba/scores.py`, `news.py`, `alerts.py`, and explicit source health/feed
  modules. Everything else is pure and unit-testable.
- Fail soft on live data: a missing player or a reshaped ESPN payload must downgrade a
  scenario to `AT_RISK`/`void` with a logged reason, never crash the heartbeat.
- Times are `America/New_York`. Money is integer cents internally; format to dollars only
  at the edge.
- Keep alerts to the compact block in `alerts.format_block`. No walls of text.

## 5b. Branch-per-phase rule (set 2026-07-29)

Every improvement / Codex phase runs on its own branch cut from the trunk tip.

- Naming convention: `phase-NN-short-slug` (e.g. `phase-23-batch-executor`).
- Trunk is `codex/broad-slate-market-matcher`; do **not** commit new phase work directly to trunk.
- Work + independent verification happen on the branch. Push the branch, then merge only after verification passes.
- Rationale: this project surfaced 6+ silent defects in shipped Codex work when everything landed on a single long-lived branch. Branching contains blast radius — `git branch -D phase-N` reverts a bad phase in one command.

## 6. Porting notes (this came from a Claude Code plugin)

The signing logic and ESPN win-prob math were ported from a working Kalshi plugin
(`cle_watcher.py` / `live_signals.py`). Behavior preserved:
- RSA-PSS, SHA-256, digest-length salt; timestamp in ms; sign `ts+METHOD+path`.
- Implied prob = `yes_price_cents / 100`. DraftKings "Live" line is de-vigged for a fair prob.
The old `monitor-cron.openclaw.jsonc` (OpenClaw scheduler) is kept under `scheduler/`
for reference; `scheduler/combined-crontab.txt` is the single portable crontab that
runs anywhere. `crontab <file>` REPLACES the entire user table, so there is
deliberately one file to install, not several.
