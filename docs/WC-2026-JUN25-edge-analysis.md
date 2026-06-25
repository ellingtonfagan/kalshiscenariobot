# June 25 World Cup Edge Analysis

Snapshot: `2026-06-25T19:01:43Z` (`3:01 p.m. ET`).

This pass compares the June 25 potential-play watchlist against independent sportsbook
prices. Two-way and three-way markets are de-vigged. One-sided prop quotes are treated
only as upper bounds because their embedded margin cannot be removed reliably.

Kalshi's general taker-fee formula is `0.07 * contracts * price * (1 - price)`, rounded
up to the next cent per order. The table uses the unrounded per-contract fee load for
comparability. Actual small-order rounding can be less favorable.

## Result

**No candidate clears the bot's normal 5-percentage-point edge gate.** The research
override is not supported by this evidence. The best verified candidate is USA not to
win, but its estimated advantage is only about 3.2 points before fees and 1.5 points
after the estimated taker-fee load.

| Candidate | Kalshi ask | External fair | Gross edge | Fee-adjusted | Classification |
|---|---:|---:|---:|---:|---|
| USA does not win | 46% | 49.2% | +3.2% | +1.5% | Watch, below gate |
| Ecuador-Germany under 2.5 | 42% | 43.4% | +1.4% | -0.3% | No edge after fees |
| Paraguay-Australia BTTS No | 57% | 57.5% | +0.5% | -1.2% | No edge after fees |
| Paraguay-Australia draw | 43% | 42.5% | -0.5% | -2.3% | No edge |
| Turkiye-USA BTTS | 59% | 57.8% | -1.2% | -2.9% | Negative |
| Netherlands by 3+ | 51% | 49.5% | -1.5% | -3.3% | Negative |
| Japan-Sweden over 2.5 | 51% | 48.4% | -2.6% | -4.3% | Negative |
| Germany wins | 62% | 59.1% | -2.9% | -4.5% | Negative |
| Ivory Coast 3+ team goals | 57% | 52.1% | -4.9% | -6.6% | Negative |

## Confirmed Lineups

- Germany did not make the anticipated wholesale rotation. Neuer, Kimmich, Pavlovic,
  Nmecha, Sane, Musiala, Wirtz, and Havertz all start. This weakens the original under
  thesis; the post-lineup external market still leaves only a 1.4-point gross under edge.
- Ivory Coast starts a strong attacking group including Diallo, Pepe, Bonny, and Yan
  Diomande. This supports the scoring scenario, but not the 57-cent price.

## Unverified Props

- **Japan-Sweden 10+ corners:** RotoWire's +115 quote implies 46.5% before removing
  vig, versus a 44% Kalshi ask. That caps the apparent gross advantage at 2.5 points
  and the estimated post-fee advantage below 1 point. It cannot clear the normal gate.
- **Cody Gakpo scores:** A -110 FanDuel quote implies 52.4% before removing vig, versus
  a 51% Kalshi ask. The 1.4-point upper-bound advantage is smaller than estimated fees.
- **Ivory Coast 7+ corners:** Independent previews support territorial dominance, but
  no paired 6.5-corner sportsbook price was available. The edge remains unverified.

## Price Targets

These are the highest whole-cent asks that would satisfy the existing 5-point gross
edge gate using the current external fair estimates. They are monitoring thresholds,
not order instructions.

| Candidate | Current ask | Maximum gate price |
|---|---:|---:|
| Ivory Coast 3+ team goals | 57 cents | 47 cents |
| Ecuador-Germany under 2.5 | 42 cents | 38 cents |
| Germany wins | 62 cents | 54 cents |
| Japan-Sweden over 2.5 | 51 cents | 43 cents |
| Netherlands wins by 3+ | 51 cents | 44 cents |
| Turkiye-USA BTTS | 59 cents | 52 cents |
| USA does not win | 46 cents | 44 cents |
| Paraguay-Australia draw | 43 cents | 37 cents |
| Paraguay-Australia BTTS No | 57 cents | 52 cents |

## Interpretation

Robust research added value here by rejecting narrative-driven entries. Netherlands
margin, Japan-Sweden goals, and the Paraguay-Australia draw all fit the game scripts,
but their current prices already reflect those stories. The edge override should not
convert agreement with a scenario into an assumed pricing advantage.

The only verified positive gross edge worth monitoring is USA not to win. It still
requires the anticipated USA rotation and a price of 44 cents or lower to meet the
normal gate. At 46 cents it remains a watch candidate, not an approved play.

## Sources

- [ESPN match odds and lineups](https://www.espn.com/soccer/scoreboard/_/league/fifa.world)
- [The Standard: confirmed Germany XI](https://www.standard.co.uk/sport/football/germany-xi-vs-ecuador-starting-lineup-confirmed-team-news-injury-latest-for-world-cup-2026-today-b1287422.html)
- [The Standard: confirmed Ivory Coast XI](https://www.standard.co.uk/sport/football/ivory-coast-xi-vs-curacao-starting-lineup-confirmed-team-news-injury-latest-for-world-cup-2026-today-b1287461.html)
- [FOX: Netherlands-Tunisia odds](https://www.foxsports.com/stories/soccer/2026-world-cup-netherlands-tunisia-odds-prediction-picks)
- [FOX: Paraguay-Australia odds](https://www.foxsports.com/stories/soccer/2026-world-cup-paraguay-australia-odds-prediction-picks)
- [OddsTrader: Turkiye-USA odds](https://www.oddstrader.com/betting/turkiye-vs-usmnt-odds-betting-preview/)
- [RotoWire: June 25 slate](https://www.rotowire.com/soccer/article/2026-world-cup-best-bets-today-7-picks-for-thursday-june-25-119628)
- [Betting.Bet: Curacao-Ivory Coast team total](https://betting.bet/uk/article/2026-06-24/fifa-world-cup-curacao-v-ivory-coast-match-preview-tips-odds)
- [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Kalshi World Cup markets](https://kalshi.com/markets/sports/soccer)

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
