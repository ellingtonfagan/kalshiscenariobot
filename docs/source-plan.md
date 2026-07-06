# Slate Discovery And Research Source Plan

This is the source hierarchy to use before building `slate-discovery` and
`research-agent`. The machine-readable version lives in `config/sources.yaml`.

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

1. Run `source-check` and fail soft if a noncritical source is missing.
2. Pull active Kalshi sports markets and order-book candidates.
3. Pull SportsGameOdds `/events` with `oddsAvailable=true` for supported league IDs.
4. Pull The Odds API `/sports` and `/sports/{sport}/odds` as a consensus backup.
5. Use ESPN scoreboard endpoints only when a configured sport adapter needs schedule or
   score fallback.
6. Normalize each candidate into a `SlateEvent`:
   `sport`, `league`, `event_id`, `start_time`, `teams`, `markets`, `source_ids`,
   `kalshi_tickers`, `liquidity`, `spread`, `consensus_odds`, `source_confidence`.
7. Write `data/<DATE>.slate_candidates.json` and only pass candidates with a Kalshi
   ticker, fresh quotes, and at least one structured sports source into research.

## `research-agent` Data Flow

1. Load a `SlateEvent` and its mapped Kalshi ticker/order book.
2. Build an evidence bundle:
   structured odds, line movement, order-book depth, current positions, injury/news
   context, and opinion/context snippets.
3. Score source confidence:
   `canonical` for Kalshi, `high` for structured odds feeds, `medium_low` for ESPN
   fallback, `low` for social/opinion.
4. Require at least two structured/non-social sources before creating a candidate thesis.
5. Record disagreement explicitly: model vs market, book consensus vs Kalshi, stale
   books, thin order book, contradictory injury/news context, and public narrative risk.
6. Write `data/<EVENT_ID>.research_bundle.json`.
7. Hand off only normalized candidates to the existing risk gate. Research never bypasses
   stale quote checks, spread checks, SGP-adjusted probability, exposure caps, live gates,
   or the kill switch.

## Provider Notes

- SportsGameOdds uses `https://api.sportsgameodds.com/v2` and the `/events` endpoint
  with API-key authentication.
- The Odds API v4 uses `https://api.the-odds-api.com/v4`; `/sports` lists in-season
  sports and `/sports/{sport}/odds` returns upcoming/live games and bookmaker odds.
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
