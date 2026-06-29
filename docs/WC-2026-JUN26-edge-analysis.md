# June 26 World Cup Edge Analysis

Snapshot: `2026-06-26T15:49:08Z` (`11:49 a.m. ET`).

This pass reviews the six-game June 26 World Cup slate against live Kalshi prices and
independent market/model references. It is research only: no orders, no parlay, and no
stake recommendation. Single-market candidates do not require SGP adjustment.

Method: de-vig paired two-way and three-way sportsbook prices where available. Treat
one-sided scorer/team-total/margin prices only as upper bounds. Fee-adjusted edge uses
Kalshi's general taker-fee load estimate, `0.07 * P * (1 - P)`, before small-order
rounding. The bot's normal gate is `+5.0%` gross edge before execution risk checks.

## Result

One candidate has robust support at the current snapshot:

| Candidate | Kalshi ask | External fair | Gross edge | Fee-adjusted | Classification |
|---|---:|---:|---:|---:|---|
| Spain wins by 2+ vs Uruguay | 32% | 38.6% | +6.6% | +5.1% | Clears normal gate |

Other positive candidates are not clean enough to treat as an execution board:

| Candidate | Kalshi ask | External fair | Gross edge | Fee-adjusted | Classification |
|---|---:|---:|---:|---:|---|
| Uruguay-Spain over 2.5 | 43% | 49.1% | +6.1% | +4.4% | Source-divergence watch |
| Spain moneyline | 59% | 64.7% | +5.7% | +4.0% | Less efficient than spread |
| Egypt moneyline | 40% | 44.1% | +4.1% | +2.4% | Watch, below gate |
| Egypt-Iran over 2.5 | 33% | 37.0% upper bound | +4.0% max | +2.5% max | Unverified one-sided edge |
| Belgium wins by 3+ | 43% | 45.7% upper bound | +2.7% max | +0.9% max | Below gate |
| Senegal-Iraq BTTS No | 55% | 56.3% | +1.3% | -0.4% | No edge after fees |

Everything else reviewed is negative or already priced in.

## Match Notes

### Norway vs France

France is the deserved favorite, but the current `61c` Kalshi ask is basically fair
against FOX/FanDuel's `-175 / +420 / +360` three-way market after de-vigging. The goals
markets look worse: Over 2.5 is `66c` on Kalshi against a de-vigged external fair near
`58.6%`, and BTTS Yes is `65c` against a fair near `54.7%`. This is a strong game script,
not a price edge.

### Senegal vs Iraq

Senegal should control the match, and independent previews agree with a margin script.
The price is the issue. Senegal -1.5 is `60c` on Kalshi against a de-vigged FOX/FanDuel
fair of `56.9%`; Over 3.5 is also slightly rich. Senegal -2.5 has qualitative support,
but available one-sided prices do not verify enough edge. BTTS No is the best measured
number but does not survive fees.

### Cape Verde vs Saudi Arabia

Cape Verde is the cleaner story: two strong group-stage draws, Saudi Arabia outshot
heavily, and both FOX and Covers lean Cape Verde. At the current `37c`, however, Cape
Verde ML is above the de-vigged fair from the current three-way market. Under 2.5 and
BTTS are also close to fair or negative after fees. Keep this as a scenario read, not
an edge.

### Uruguay vs Spain

This is the slate's cleanest market mismatch. FOX/FanDuel lists Spain -1.5 at `+145`
with Uruguay +1.5 at `-185`, a de-vigged fair near `38.6%`; Kalshi asks only `32c` for
Spain to win by more than 1.5 goals. Covers independently makes Spain -1.5 its top pick
and says it is playable down to `+125`, which is materially shorter than the Kalshi
price equivalent.

Spain ML and match Over 2.5 also screen positive versus FOX/FanDuel, but they are less
robust. The moneyline is inefficient relative to the spread, and the total has source
divergence because another external odds snapshot is much closer to Kalshi.

### New Zealand vs Belgium

Belgium should dominate territory, but the market is not loose enough. Dimers gives
Belgium an `81.0%` win probability against an `84c` Kalshi ask. Belgium -2.5 has a
one-sided Covers price of `+119`, but that only caps fair probability near `45.7%`
before vig, versus `43c` on Kalshi. That is below the 5-point gate and not de-vigged.

### Egypt vs Iran

Opta's model, reported by Al Jazeera, gives Egypt a `44.1%` win probability, Iran
`24.6%`, and draw `31.3%`. Egypt ML at `40c` is the best late-game watch candidate, but
it is still below the bot's 5-point gross edge gate. The Over 2.5 thesis has Covers
support, but the available price is one-sided and cannot be treated as a fully verified
edge.

## Price Targets

These are maximum asks for the selected side to clear the normal 5-point gross edge
gate using the current fair estimate. They are monitoring thresholds, not order
instructions.

| Candidate | Current ask | Gate price |
|---|---:|---:|
| Spain wins by 2+ | 32c | 33c |
| Spain moneyline | 59c | 59c |
| Uruguay-Spain over 2.5 | 43c | 44c |
| Egypt moneyline | 40c | 39c |
| Egypt-Iran over 2.5 | 33c | 32c upper-bound target |
| Belgium wins by 3+ | 43c | 40c upper-bound target |
| Senegal-Iraq BTTS No | 55c | 51c |
| Senegal -1.5 | 60c | 51c |
| Cape Verde ML | 37c | 30c |
| Cape Verde-Saudi under 2.5 | 57c | 50c |
| Norway-France France ML | 61c | 55c |
| Norway-France over 2.5 | 66c | 53c |

## Sources

- [Kalshi World Cup markets](https://kalshi.com/category/sports/soccer/fifa-world-cup)
- [SportsBettingDime: June 26 six-game model slate](https://www.sportsbettingdime.com/news/soccer/world-cup-picks-predictions-today-best-bets-for-all-6-games-on-june-26/)
- [FOX: France vs Norway odds](https://www.foxsports.com/stories/soccer/2026-world-cup-france-norway-odds-prediction-picks)
- [FOX: Senegal vs Iraq odds](https://www.foxsports.com/stories/soccer/2026-world-cup-senegal-iraq-odds-prediction-picks)
- [FOX: Spain vs Uruguay odds](https://www.foxsports.com/stories/soccer/2026-world-cup-spain-uruguay-odds-prediction-picks)
- [FOX: Saudi Arabia vs Cape Verde odds](https://www.foxsports.com/stories/soccer/2026-world-cup-saudi-arabia-cape-verde-odds-prediction-picks)
- [Covers: Spain vs Uruguay](https://www.covers.com/world-cup/spain-vs-uruguay-prediction-picks-odds-friday-6-26-2026)
- [Covers: Cape Verde vs Saudi Arabia](https://www.covers.com/world-cup/cape-verde-vs-saudi-arabia-prediction-picks-odds-friday-6-26-2026)
- [Covers: Belgium vs New Zealand](https://www.covers.com/world-cup/belgium-vs-new-zealand-prediction-picks-odds-friday-6-26-2026)
- [Covers: Egypt vs Iran](https://www.covers.com/world-cup/egypt-vs-iran-prediction-picks-odds-friday-6-26-2026)
- [Al Jazeera: Egypt vs Iran Opta probabilities](https://www.aljazeera.com/sports/2026/6/26/egypt-vs-iran-world-cup-2026-prediction-kickoff-schedule-teams)
- [Dimers: Belgium vs New Zealand probabilities](https://www.dimers.com/bet-hub/swc/schedule/2026_3_nzl_bel)

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
