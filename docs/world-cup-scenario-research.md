# World Cup Scenario Research

Use this process before publishing or executing a soccer scenario.

## Timing

1. Capture an opening snapshot for line movement.
2. Refresh no earlier than 90 minutes and no later than 60 minutes before kickoff.
3. Confirm official lineups, formation changes, injuries, suspensions, venue, and weather.
4. Mark any scenario created after kickoff as `in_play`; never mix it into pregame calibration.

## Probability Baseline

1. De-vig the three-way winner market instead of reading one contract in isolation.
2. Fit each team's expected goals from multiple team-total thresholds with
   `soccer_research.fit_expected_goals`.
3. Calculate the scenario as a set of valid scorelines with
   `soccer_research.scoreline_probability`.
4. Apply an explicit uncertainty multiplier, normally `0.90`, for lineup, model, and
   independent-Poisson error.
5. Keep market and model probabilities separate. A coherent story is not automatically
   a trade; execution still requires the configured minimum edge.

## Scenario Construction

- Express the scenario as scoreline conditions before selecting market legs.
- Avoid decorative legs that are already implied by another leg.
- Treat nested legs as strongly dependent. Never multiply their probabilities naively.
- Include one counter-scenario for the favorite failing to break a low block.
- Risk 5 is a hope bet and must be labeled explicitly.
- Prefer liquid winner, primary total, BTTS, and first spread markets.
- Reject wide or empty order books even when the narrative is attractive.

## Review

After settlement, record:

- final score and source;
- each leg result;
- scenario hit or miss;
- whether the snapshot was genuinely pregame;
- Brier score for the adjusted joint probability;
- what information was missing at publication time;
- one concrete rule change for the next slate.

Do not count an in-play scenario as evidence that a pregame model was calibrated.
