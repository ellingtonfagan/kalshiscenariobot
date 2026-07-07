# Slate Discovery And Research Source Plan

This is the source hierarchy used by `slate-discovery`, `slate-verify`,
`market-matcher`, `candidate-ranker`, and `research-agent`. The machine-readable version
lives in `config/sources.yaml`.

## Source Hierarchy

1. **Kalshi** is the execution venue and order book source. It is canonical for tradable
   tickers, quotes, depth, positions, balances, fills, and settlement checks.
2. **SportsGameOdds** is the primary structured sports feed for candidate events, live
   and pregame odds, line movement, player props, and cross-book context.
3. **The Odds API** is the secondary structured odds feed. Use it to cross-check event
   identity, consensus prices, and major markets.
4. **ESPN public site API** is fallback schedule, score, and news context only. It is
   undocumented, so do not use it as the primary odds source or as an execution trigger.
5. **Reddit, YouTube, X, and Bluesky** are opinion/context feeds only. They can surface
   narratives, crowd disagreement, beat-reporter notes, and stale-market explanations,
   but they must never be the sole trigger for execution.

## `slate-discovery` Data Flow

1. `source-check` activates `slate-discovery` when source readiness has been recorded.
   With network checks on, it also runs a read-only Kalshi scope probe: open markets
   scanned, in-scope sports markets, nonzero quoted markets, and sport buckets.
2. Pull broad open Kalshi markets first. This is the starting universe, not the old
   configured game file.
3. Classify those Kalshi markets into in-scope buckets such as World Cup/soccer, MLB,
   NBA Summer League, WNBA, NFL, NHL, tennis, and MMA.
4. Write `sport_market_candidates.json` with top open Kalshi tickers so book-watch can
   inspect depth quickly.
5. Pull SportsGameOdds `/events` with `oddsAvailable=true` for supported league IDs.
6. Pull The Odds API `/sports/{sport}/odds` as a consensus backup for `h2h`,
   `spreads`, and `totals`.
7. Use ESPN scoreboard endpoints only when a configured sport adapter needs schedule or
   score fallback.
8. Attach active Kalshi catalog rows and mapped scenario markets through the sport
   adapter.
9. Normalize each candidate into a `SlateEvent`:
   `sport`, `league`, `event_id`, `start_time`, `teams`, `markets`, `source_ids`,
   `kalshi_tickers`, `liquidity`, `spread`, `consensus_odds`, `source_confidence`.
10. Write `data/<GAME_ID>.slate_candidates.json` and only pass candidates with a Kalshi
   ticker, fresh quotes, and at least one structured sports source into research.

Line-selection reasoning:

- A line must start with an open Kalshi ticker before it can be more than watchlist.
- A Kalshi ticker must be matched to comparable live sportsbook odds before it can be
  priced for edge.
- SportsGameOdds is the primary structured line source; The Odds API is the backup
  consensus source.
- ESPN fallback can help identify the event or score state, but it cannot make a line
  trade eligible.
- Social/opinion feeds can explain narratives or disagreement, but cannot make a line
  trade eligible.
- Lines with missing Kalshi mapping, no structured sportsbook source, duplicate event
  identity, or only fallback/social support are rejected by `slate-verify`.

## `slate-verify` Data Flow

1. Refresh `slate_candidates.json` first if it is stale.
2. Load `slate_candidates.json`.
3. Reject ESPN-only, social-only, duplicate, unmapped, or Kalshi-missing candidates.
4. Flag candidates that need more learning-log/backtest coverage before autonomous
   sizing confidence.
5. Write `slate_verification.json` with `approved_for_research`,
   `approved_for_execution`, and rejection reasons.

## `market-matcher` Data Flow

1. Refresh `slate_candidates.json`, `sport_market_candidates.json`, and `book_watch.json`
   if any handoff is stale.
2. Load current order-book metrics and the previous stored order-book metrics for each
   ticker.
3. Match open Kalshi slate markets to comparable structured odds/API candidates using
   conservative token overlap.
4. Record order-book deltas: bid change, ask change, spread change, top depth, fillable
   depth, and VWAP movement.
5. Write `market_matches.json` with every matched/watchlist row.
6. Write `execution_slate.json` with only rows marked `execution_review`.

`execution_review` is not approval to trade. It only means the current order book is
fresh enough, narrow enough, and visible enough for the research agent to review. Model
probability, SGP-adjusted probability, research approval, and `risk.py` are still required
before execution.

## `candidate-ranker` Data Flow

1. Refresh `slate_candidates.json` and `market_matches.json` if either handoff is stale.
2. Resolve Kalshi and sportsbook rows into deterministic identities:
   sport, event key, market type, line, side, and start date.
3. For Kalshi multivariate exchange markets, use `mve_selected_legs` and
   `custom_strike.Associated Markets` to resolve each component leg. A composite market
   is exact only when every supported leg has an exact external identity match.
4. Prefer exact identity matches; fuzzy matches may be reported but never pass edge.
5. Convert sportsbook prices to implied probabilities, de-vig each book market, exclude
   outlier books with explicit reporting, and compute a weighted consensus probability.
   Composite probabilities are reported as SGP-adjusted joint probabilities with a
   conservative generic haircut until settlement learning can calibrate them.
6. Compare consensus probability to executable Kalshi order-book price.
7. Raise required edge when book count is thin, books disagree, or spread is wide.
8. Write `candidate_ranker.json` for all ranked rows and `edge_candidates.json` for rows
   that pass the edge model.

This phase still does not place orders. Broad-slate live execution remains off unless a
future explicit broad-slate execution gate is added and enabled.

The diagnostics block counts slate coverage, line-market coverage, structured source
coverage, Kalshi/external event-key resolution, exact/fuzzy/none identity outcomes, and
component-level composite matching. A July 6, 2026 live run against 200 Kalshi rows
resolved 200 composite markets, 526 exact legs, 43 full composite matches, 107 partial
composite matches, and 43 SGP-adjusted model probabilities.

## `research-agent` Data Flow

1. `candidate-ranker` activates `research-agent` after fresh quote, order-book, matcher,
   and edge artifacts exist.
2. Refresh mapped Kalshi quote snapshots, slate verification, market matching, and
   candidate ranking if any handoff is stale.
3. Load a `SlateEvent` and its mapped Kalshi ticker/order book.
4. Build an evidence bundle:
   structured odds, line movement, order-book depth, current positions, injury/news
   context, and opinion/context snippets.
5. Score source confidence:
   `canonical` for Kalshi, `high` for structured odds feeds, `medium_low` for ESPN
   fallback, `low` for social/opinion.
6. Require at least two structured/non-social sources before creating a candidate thesis.
7. Record disagreement explicitly: model vs market, book consensus vs Kalshi, stale
   books, thin order book, contradictory injury/news context, and public narrative risk.
8. Write `data/<GAME_ID>.research_bundle.json`.
9. Write `market_candidates.json` with only `trade_eligible` rows so downstream agents
   can quickly verify/update the candidate set.
10. Activate only normalized candidates for the existing risk gate. Research never bypasses
   stale quote checks, spread checks, SGP-adjusted probability, exposure caps, live gates,
   or the kill switch.

## Provider Notes

- SportsGameOdds uses `https://api.sportsgameodds.com/v2` and the `/events` endpoint
  with API-key authentication.
- Captured SportsGameOdds event fixtures use `body.data`, `teams.away/home.names.*`,
  and `odds.*.byBookmaker`; bookmaker keys in the captured MLB/NFL fixtures included
  DraftKings, FanDuel, BetMGM, Caesars, ESPNBet, Bovada, PointsBet, Unibet, and
  William Hill, but not Pinnacle or Circa.
- The Odds API v4 uses `https://api.the-odds-api.com/v4`; `/sports` lists in-season
  sports and `/sports/{sport}/odds` returns upcoming/live games and bookmaker odds.
  The configured regions are `us,eu`; the captured fixtures include Pinnacle from the
  EU region but not Circa.
- ESPN hidden endpoints are useful for schedule/score/news fallbacks, but they are not
  documented as a stable production contract.
- Reddit should use official API access only. Do not scrape private, paywalled, or
  restricted communities.

## Execution Boundary

No source in this document is allowed to place an order. Execution remains isolated to
`demo-execute` and `live-execute`, and live execution still requires:

```bash
NBABOT_EXECUTION_MODE=live
NBABOT_DRY_RUN=0
NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY
```

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
