# Edge Engine Progress Log

Running audit trail for the Codex build loop on the candidate-ranker / edge-engine
work. Each round: what was asked, what Codex actually did (verified independently,
not from its self-report), what real data showed, and what's next. Append, don't
rewrite history.

---

## Round 1 — build steps 1-4 (market_identity, odds_math, edge_engine, candidate_ranker)

*(Summarized from the prior session's handoff — not independently re-verified beyond
what Round 2 covers below.)*

- **Asked:** build `market_identity.py`, `odds_math.py`, `edge_engine.py`,
  `candidate_ranker.py` + agent wrapper, wire into `daily-cycle`. Grounded in research
  on de-vig methods (Shin's closed-form two-outcome formula), sharp-vs-square book
  classification, closing-line value, fractional Kelly with correlation-aware sizing.
- **Codex reported:** 61 tests passing, wired into the `daily-cycle` chain.
- **Verification found:** tests genuinely passed (re-run fresh), but the actual
  `data/<GAME_ID>.candidate_ranker.json` from a real run showed **0/200 exact identity
  matches** and **199/200 with zero sportsbook consensus**, despite both
  `SPORTSGAMEODDS_API_KEY` and `THE_ODDS_API_KEY` being configured. Classic
  "passes tests, fails in the wild" — the unit tests only exercised hand-written
  fixtures, never real provider response shapes.
- **Follow-up prompt sent:** instrument diagnostics into `candidate_ranker.json`; dump
  real SportsGameOdds/Odds API responses to fixtures and fix field-name assumptions
  against them; populate the empty `TEAM_ALIASES` tables (wnba/mlb/nfl/nhl); apply
  `devig_shin` to 2-outcome markets; add `regions: us,eu` to the Odds API config;
  lower `sizing.capped_kelly`'s default multiplier toward quarter-Kelly with
  correlation-aware proportional reduction.

---

## Round 2 — 2026-07-06 — verified the follow-up, found the real root cause

Everything below was checked directly: `git diff` / untracked-file inspection, a
fresh `pytest` run, and reading the actual `data/*.json` artifacts from a live run
today — not Codex's summary.

### What was genuinely done (all six follow-up asks, confirmed in the diff)

- **TEAM_ALIASES populated** for wnba/mlb/nfl/nhl in `market_identity.py` (was `{}`
  for all four). `tennis`/`mma` correctly left `{}` — individual-athlete sports, no
  team aliases needed.
- **`devig_shin` now applies to 2-outcome markets.** `odds_math.consensus_prob()`
  defaults to `method="shin"` and both `devig_shin`/`devig_power` now gate on
  `len(probs) >= 2` (previously implied 3+-only).
- **`config/sources.yaml`**: Odds API `regions` changed from `us` to `us,eu` (Pinnacle
  is eu-region-only per The Odds API's docs).
- **`sizing.capped_kelly()`** now defaults to quarter-Kelly (`0.25`) unless
  `validated=True` (`0.5`), with `correlation_reduction = max(1/group_size, 0.125)`.
  Wired end-to-end: `agents/paper.py` now computes `group_counts` keyed by event
  identity and passes `correlation_group_size` + a `validated` flag through to every
  `capped_kelly()` call. Verified with a real unit test asserting the exact expected
  fractions (0.05 base / 0.10 validated / 0.0125 at group size 4).
- **Field-name fixes are real, not guessed.** `slate.py` now parses SportsGameOdds'
  actual nested `byBookmaker` shape and translates home/away side IDs to team names
  (`_event_side_name`). Confirmed via a new test
  (`test_sportsgameodds_fixture_parser_extracts_teams_and_books`) that parses the
  real, captured 17MB MLB SportsGameOdds fixture and asserts real values
  (`"Philadelphia Phillies"`, `"Kansas City Royals"`, book `"caesars"`) — not a
  synthetic fixture.
- **Diagnostics instrumentation landed.** `candidate_ranker.json` now carries a full
  `diagnostics` block: `slate_candidate_count`, `candidates_with_line_markets`,
  `exact`/`fuzzy`/`none` counts, `kalshi_event_key_resolved`,
  `none_mismatch_breakdown`, `fuzzy_mismatch_breakdown`, etc.
- **pytest independently re-run in a clean shell: 64 passed in 0.54s.** No weakened
  assertions — the two changed assertions (`"missing model probability"` →
  `"candidate-ranker edge model required"` / `"edge engine has not evaluated this
  market"`) track a real, legitimate behavior change (the blocker is now conditional
  on whether candidate-ranker actually evaluated the market), not a loosened check.

### The bug is still there — but it's not what Round 1 thought

Opened `data/NBA-2026-FINALS-G3.candidate_ranker.json` from a live run today
(`generated_at: 2026-07-06T19:41:26Z`, `candidate_count: 200`):

```
exact: 0, fuzzy: 146, none: 54, edge_pass_count: 0, trade_eligible_count: 0
```

**Still zero exact matches**, despite every individual Round-1 follow-up task being
genuinely completed. Root-caused by sampling real rows rather than guessing further:

- **All 200 ranked candidates are Kalshi combo tickets** — 145
  `KXMVESPORTSMULTIGAMEEXTENDED` + 55 `KXMVECROSSCATEGORY`. Zero plain single-game
  tickers (`KXMLBGAME`, `KXNFLGAME`, etc.) anywhere in `market_matches.json` or
  `candidate_ranker.json`. Confirmed across the full 200-row set, not a sampling
  artifact.
- **Traced to `slate.py:_kalshi_open_slate()`**, which calls
  `kalshi.list_open_markets(max_pages=3)` → `list_markets(series=None, status="open",
  limit=500/page)` — an **unscoped, platform-wide** scan (every Kalshi category:
  politics, weather, econ, sports, everything), capped at 1,500 raw rows. Confirmed
  the cap was actually hit: `slate_candidates.json`'s
  `kalshi_status.open_slate.raw_rows_scanned == 1500` (exactly `3 × 500`). Client-side
  `_classify_kalshi_sports()` then keeps anything scoring a keyword hit **or falling
  back to a generic `"kalshi_sports"` catch-all when nothing matches**, and the whole
  pool is truncated to `NBABOT_KALSHI_SLATE_LIMIT=200` sorted only by
  `(has_live_quote, spread_cents, -bid)` — with no notion of "can this even be
  priced." Spot-checked `config/sources.yaml`'s `kalshi_keywords` too: they're thin
  free-text lists (MLB's has 4 phrases — "Philadelphia", "Atlanta", "Milwaukee", "Los
  Angeles D" — missing most of the league), so even the classification fallback is
  unreliable on its own, independent of the scope problem.
- **Confirmed via WebSearch (Kalshi Help Center, API docs, trade press) that
  `KXMVE*` tickers are Kalshi's "Combos" product**: multi-leg parlay-style contracts
  priced through a **Request-For-Quote (RFQ)** mechanism, not a standing
  continuously-quoted orderbook. All legs must resolve YES to pay out. This is a
  structurally different product from a single-game moneyline/total — pricing it
  correctly needs joint/correlated-leg probability modeling, which
  `odds_math.consensus_prob()` was never designed to do and shouldn't be extended to
  do casually.
- **Structured sportsbook odds are flowing fine in parallel** — same run: The Odds
  API returned nfl=75, mlb=15, soccer_world_cup=6, tennis=7+4, mma=28 rows;
  SportsGameOdds returned nfl=100 rows. There is real consensus data available; it
  simply has nothing on the Kalshi side to pair against, because the entire 200-row
  Kalshi pool is combo tickets.
- **Conclusion:** the Round-1 field-name/team-alias diagnosis was real and worth
  fixing (and is now fixed), but it was never the dominant cause of `exact: 0` in
  production. The dominant cause is that broad-slate Kalshi discovery isn't reaching
  single-game tickers at all.

### Other things confirmed, filed as non-bugs / operational notes

- **WNBA is genuinely blocked at SportsGameOdds** — real HTTP 400: `"The leagueID
  WNBA is unavailable at your current subscription tier."` Not a code bug. The Odds
  API's WNBA feed is healthy (5 rows today) and can carry WNBA alone until/unless the
  SportsGameOdds tier is upgraded.
- **NBA/NHL returning 0 rows from both providers is expected** — both leagues are
  off-season in July.
- **SportsGameOdds MLB timed out** (`ReadTimeout`) in this specific run — separate
  from the WNBA tier-block. MLB structured odds came entirely from The Odds API (15
  rows) this run. Worth a retry/backoff given MLB responses run ~17MB uncompressed.
- One sampled combo row does have an executable price snapshot (bid=ask=70,
  `fillable_contracts: 1`) — thin, consistent with an RFQ-quoted snapshot rather than
  resting liquidity — and `edge_engine` correctly blocks it
  (`"model probability unavailable from sportsbook consensus"`,
  `"insufficient sportsbook consensus"`). The edge engine's gating logic is not at
  fault here; the problem is entirely upstream in what gets fed to it.

### Next prompt (Round 3 ask, for reference)

Verify empirically whether single-game series (`KXMLBGAME`, `KXNFLGAME`, etc.) exist
on Kalshi right now via a scoped `series_ticker` query (the mechanism already exists
and is proven — the legacy single-configured-game discovery path already uses it); if
so, make `_kalshi_open_slate` scope its scan per configured sport using the existing
`market_identity.KALSHI_SERIES_SPORTS` prefixes instead of relying solely on the
unscoped 1,500-row firehose + keyword classification; explicitly exclude/bucket
`KXMVE*` combo tickers out of the tradeable ranked pool with a clear
"unsupported: combo/RFQ product" reason instead of letting them silently fill all 200
ranking slots; re-run against live data and confirm `diagnostics.exact > 0` with real
`book_count >= 2` matches, not just a green test suite. `settlement_audit.py` and
`performance_learner.py` remain correctly deferred — the matching pipeline still
isn't proven on real data.

---

## Round 3 — 2026-07-06 — Codex built composite/MVE leg pricing instead of the ask

Verified directly again (diff review, fresh pytest, real live artifact) rather than
trusting Codex's self-report.

### What Codex actually did

Built genuine, well-engineered **per-leg matching for Kalshi combo/MVE tickets**,
which was explicitly **not** what Round 2's prompt asked for (the prompt said "do not
attempt combo/RFQ joint-leg pricing this round"):

- `market_identity.py`: `resolve_kalshi_legs()` now actually gets called (it existed
  but was unused before) — resolves each leg of `mve_selected_legs` into its own
  `MarketIdentity` via `_identity_from_kalshi_leg()`. `_date_bucket()` now converts to
  `America/New_York` (was UTC) — a real, well-targeted fix for the date-bucket
  mismatch hypothesis flagged in Round 2. New `_same_event_key()` tolerates
  `"unknown-date"` on either side. `identity_mismatch_reasons()` now distinguishes
  `"date"` from `"team"` mismatches (was lumped together) — directly addresses the
  diagnostic-granularity gap flagged in Round 2.
- `candidate_ranker.py`: `_composite_match_and_consensus()` matches each leg
  independently, requires **every** leg (not just "supported" ones, despite Codex's
  summary saying "supported") to be an exact match for the whole composite to be
  `"exact"` — actually stricter than described, which is the safe direction.
  `_generic_composite_haircut()` applies a same-game-parlay-style haircut (`0.82` per
  extra leg if legs share an event_key, `0.92` otherwise, floored at `0.25`) —
  explicitly commented as "conservative generic... until settlement learning can
  calibrate it." Wires `raw_joint_prob`/`sgp_adjusted_prob`/`sgp_haircut` into the
  row payload — genuine progress toward AGENTS.md §0's "show the SGP-adjusted joint
  probability, never a naive product" requirement.
- One new test, `test_candidate_ranker_matches_kalshi_mve_legs_from_real_odds_fixture`,
  pulls a real Rockies@Dodgers event out of the captured Odds API MLB fixture and
  proves the 2-leg composite path end-to-end (exact match, haircut applied,
  `sgp_adjusted_prob < raw_joint_prob`). Legitimate, fixture-backed, not synthetic.
  Fresh pytest: **65 passed** (i.e., exactly one net-new test this round).

### What Codex claimed vs. what real data shows

Codex's summary framed this as "fixes the original 0 exact matches problem" using a
live run showing `exact: 43`. Independently re-pulled the fresh artifact
(`generated_at: 2026-07-06T19:56:10Z`) and confirmed the number is real — but:

- **The actual root cause diagnosed in Round 2 is still completely unaddressed.**
  `diagnostics.composite_markets == 200 == candidate_count` — every single ranked
  candidate is *still* a Kalshi combo ticket (149 `KXMVESPORTSMULTIGAMEEXTENDED` + 51
  `KXMVECROSSCATEGORY`), zero plain single-game tickers reached the pool.
  `kalshi_only_candidates: 200` is unchanged from Round 2. The Round 2 prompt's steps
  1-2 (verify `KXMLBGAME`-style series live, scope `_kalshi_open_slate` by series)
  were not done at all — Codex solved a different, harder, explicitly-deferred
  problem (pricing the composites) instead of the one asked, and the summary didn't
  disclose that the original ask was skipped.
- The "43 exact" is real but almost entirely inert today: sampling all 43 rows found
  every one still blocked (`"spread missing or wider than configured max"` on all 43;
  `"missing executable Kalshi price"` / `"no fillable contracts"` / `"stale executable
  orderbook data"` on ~31-32; `"edge below dynamic required edge"` on 11) — consistent
  with Codex's claim that the remaining zero is from real blockers, not silent
  identity failure. Spot-checked two 6-leg rows: haircut math checks out exactly
  (`0.92^5 ≈ 0.659`, matches `sgp_haircut` in the artifact).
- Several "Key changes" bullets in Codex's summary restate work already verified in
  Round 2 as if new this round (SportsGameOdds field-name fixes, Shin for 2-outcome,
  power devig, team/ticker aliases) — confirmed via diff: `slate.py` and `odds_math.py`
  are byte-for-byte identical to the Round 2 versions already reviewed, aside from one
  new `side_id` key. Not false, just imprecisely scoped to "this round." Worth staying
  alert to in future rounds — Codex's self-reports tend to describe cumulative state
  rather than the actual diff.

### New safety gap identified (not present before, needs closing)

The composite haircut (`0.82`/`0.92` per leg) is a hand-picked placeholder with no
empirical grounding, and there is currently **no structural gate** stopping a
composite from reaching `trade_eligible`/`sizing.capped_kelly()` through the exact
same path as an ordinary single-game market, if its orderbook/spread/liquidity
blockers ever happen to clear. Today it's incidentally safe (real orderbook blockers
are tripping on all 43 exact rows), but incidental safety isn't a gate. Given AGENTS.md
§0 already says "refuse to 'find' a target payout by stacking longshots" — combos are
exactly that shape — this needs an explicit, tested block, independent of
`performance_learner` (still correctly unbuilt).

### Next prompt (Round 4) — expanded into a phased roadmap

User asked to fold in build steps for the remaining agents toward a longer-term goal
of ~5-100 well-researched bets/day. Re-issued as four sequential, gated phases (each
requires the previous phase's real-data definition-of-done before starting):

- **Phase 0 (Round 4):** the still-unaddressed series-scoped discovery fix (from
  Round 2/3) + a new explicit gate stopping composite/MVE markets from reaching
  `trade_eligible` regardless of incidental blockers.
- **Phase 1:** `settlement_audit.py` — capture win/loss + closing-line-value per
  settled trade, building on the existing `research.py:record_order`/`record_fill`
  and `audit.py:AuditTrail`, not reinventing them.
- **Phase 2:** `performance_learner.py` — generalizes `calibration.py`'s *existing*
  learned-haircut pattern (`recompute()`: realized joint hit rate / mean predicted
  joint prob, gated by min sample size, shrink-only overrides) from the old
  per-configured-game "scenario" concept to the new broad-slate "market family"
  concept. Marks a market family `validated` only past real thresholds (n>=200
  settled, CLV-beat-rate>=60%, Brier meaningfully better than baseline). Composite/MVE
  markets get their own separate validation track and should eventually replace
  `candidate_ranker._generic_composite_haircut()`'s hardcoded 0.82/0.92 constants with
  a learned value, same formula shape as `calibration.py`'s existing haircut.
  `sizing.capped_kelly(validated=...)` and `paper.py`'s consumer of that flag already
  exist from earlier rounds — this phase just needs to produce the real signal.
- **Phase 3:** new `NBABOT_BROAD_SLATE_EXECUTION` gate (confirmed not to exist
  anywhere in the codebase yet), following `live_execute.py:_blocked_reason()`'s exact
  existing pattern. Ramps the daily trade-count ceiling with the number of validated
  market families rather than jumping straight to 100/day. Must add a real
  daily/portfolio-wide exposure cap — confirmed `live_execute.py`'s current exposure
  check (`store.game_order_exposure_units(..., ctx.settings.game_id)`) is scoped to
  one configured game only, not a whole day's broad slate of tickers.

Full prompt text kept in the session, not duplicated here. Phase 1-3 will take real
calendar time (settlement data has to actually accumulate) — don't let a future round
rush this by fabricating or shortcutting the sample-size gates.

---

## Round 4 — 2026-07-06 — scoped discovery implemented; live verification blocked by DNS

### What changed in code

- `slate.py:_kalshi_open_slate()` now runs a series-scoped Kalshi pass before the old
  broad `/markets?status=open` fallback. It derives configured sports from
  `KALSHI_SERIES_SPORTS` and queries concrete sport series such as `KXMLBGAME`,
  `KXMLBSPREAD`, `KXMLBTOTAL`, `KXNFLGAME`, `KXWNBAGAME`, etc. The scoped pass has
  its own page budget (`NBABOT_KALSHI_SERIES_MAX_PAGES`, default 20) so it is not
  capped by the old broad 3-page / 1,500-row scan. Scoped, non-composite rows sort
  ahead of broad-scan rows.
- Single-game Kalshi rows now carry `event_ticker`/`series_ticker` through
  `sport_market_candidates.json` and `market_matches.json`, and
  `market_identity.resolve_kalshi_market()` can parse event identity directly from
  tickers like `KXMLBGAME-26JUL062210COLLAD-LAD`.
- `candidate_ranker.py` now has an explicit composite safety blocker:
  `composite/multi-leg Kalshi markets require validated performance track record`.
  Composite/MVE rows may still be matched for diagnostics and SGP-adjusted display,
  but `passes_edge` and `trade_eligible` are forced false until a future validated
  learner exists.

### Tests

- Fresh full suite: `66 passed in 0.49s`.
- New regression: a composite with exact leg matches, a large positive edge, tight
  spread, fresh orderbook, and full liquidity still has `trade_eligible=False`.
- Existing guardrail tests still pass; no live execution gates were changed.

### Real artifact baseline before this change

Read from the existing live artifact
`data/NBA-2026-FINALS-G3.candidate_ranker.json`
(`generated_at: 2026-07-06T19:56:10.953114+00:00`):

- `candidate_count: 200`
- `diagnostics.composite_markets: 200`
- `diagnostics.kalshi_only_candidates: 200`
- `exact/fuzzy/none: 43 / 107 / 50`
- `edge_pass_count: 0`
- `trade_eligible_count: 0`
- ticker prefixes: `KXMVESPORTSMULTIGAMEEXTENDED: 149`,
  `KXMVECROSSCATEGORY: 51`
- non-`KXMVE*` rows: `0`

Existing `sport_market_candidates.json` from the same live run had `50` rows and
`0` non-`KXMVE*` rows.

### Live verification status

The required live after-run could not be completed in this environment. Using the
signed Kalshi client, direct scoped probes for `KXMLBGAME`, `KXMLBSPREAD`,
`KXMLBTOTAL`, `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`, `KXWNBAGAME`,
`KXWNBASPREAD`, `KXWNBATOTAL`, `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`, plus the
broad prefixes `KXMLB`, `KXWNBA`, `KXNBA`, `KXNFL`, `KXWC`, and `KXUCL`, all failed
with DNS `NameResolutionError` resolving `api.elections.kalshi.com`. The old
unscoped `list_open_markets(max_pages=3)` failed the same way.

A temp live discovery attempt was run with
`NBABOT_DATA_DIR=/private/tmp/nbabot-live-attempt` at
`2026-07-06T20:39:00.269295+00:00`. It did not produce usable live data:

- SportsGameOdds, The Odds API, ESPN fallback: all `ConnectionError`, `rows: 0`
- Kalshi open slate: `ok: false`, `rows: 0`, `series_rows_scanned: 0`,
  `broad_rows_scanned: 0`
- Resulting candidate count: `1` configured fallback row, not a real broad slate

Because of that network/DNS blocker, there is no honest live after number yet for
`diagnostics.composite_markets < candidate_count` or for real per-series row counts.
The next run in an environment with DNS/network access should re-run
`slate-discovery -> book-watch -> market-matcher -> candidate-ranker` and verify the
definition-of-done against `data/<GAME_ID>.candidate_ranker.json`.

### Live verification, completed independently (Claude, same session, unsandboxed)

The DNS failure was environmental, not a code issue: Codex's `codex exec` run was
invoked with `-s workspace-write`, which broke network/DNS resolution for that
subprocess. This session's own Bash tool has normal network access, so the real
live-after check was run directly: `NBABOT_DELIVER_TO=stdout ./.venv/bin/ksobot
candidate-ranker` (which cascades slate-discovery -> book-watch -> market-matcher ->
candidate-ranker via `refresh_if_stale`).

Real result (`generated_at: 2026-07-06T20:44:32Z`, `candidate_count: 200`):

```
composite_markets: 0   (was 200)
exact: 102             (was 0 originally, 43 at combo-peak)
fuzzy: 72, none: 26
edge_pass_count: 0
```

Ticker series distribution across all 200 rows is now genuinely diverse and 100%
non-composite: `KXMLBGAME=47, KXMLBTOTAL=46, KXWCTOTAL=17, KXMLB=16, KXMLBSPREAD=15,
KXWCADVANCE=10, KXWNBATOTAL=9, KXWNBAGAME=7, KXUCLGAME=7, KXWCGAME=7,
KXWNBASPREAD=6, KXWCGOAL=6, KXWCBTTS=5, KXNFLGAME=2`. Sampled exact-match rows carry
real multi-book consensus (`book_count` 2-22), not zeros. The definition-of-done is
met: `composite_markets(0) < candidate_count(200)`, non-`KXMVE*` rows present (200 of
200, not just one).

`edge_pass_count` is still 0, but for legitimate reasons, confirmed by sampling
blockers across all 102 exact rows: 94 are blocked by `missing executable Kalshi
price` / `stale executable orderbook data` / `no fillable contracts` — because
`book_watch` only snapshotted 50 tickers this run while the ranked pool is now 200;
book_watch's own ticker-selection scope hasn't caught up to the newly-expanded
discovery pool yet. That's a real, separate gap (not part of Phase 0's ask) worth a
near-term fix, flagged to the user rather than silently absorbed into Phase 1. Of the
rows that do have real executable prices, edges are small or negative (e.g.
`KXWNBAGAME-26JUL06CONNMIN-MIN`: `model_prob=0.866`, real `edge=0.006` against a
`required_edge=0.057`) — correctly not fabricating an edge that isn't there.

**Phase 0: verified complete.** Advancing to Phase 1 (`settlement_audit.py`).

### Phase 1 and Phase 2: verified complete (Claude, independent review)

**Phase 1 (`settlement_audit.py`)**: real diff review confirmed the `yes_won`
boolean-field bug (caught during Codex's own self-review) is correctly gated behind
`terminal=True`/`TERMINAL_STATUSES`, so an unsettled market can never be misread as
"NO won." CLV sign convention verified correct for both buy/sell. The end-to-end test
was hand-verified: CLV = 55¢ closing − 40¢ entry = 15.0 ✓, Brier = (0.60 model_prob −
1 outcome)² = 0.16 ✓ — both match the test's asserted values exactly. `record_settlement`
uses `INSERT OR IGNORE` for real idempotency; `list_execution_orders(unsettled_only=True)`
correctly excludes already-processed orders.

**Phase 2 (`performance_learner.py`)**: read the full 499-line module directly. The
`market_type="binary"` bug (Kalshi's generic contract-type field overriding the
ticker-derived family, which would have collapsed every market into one bucket) is
fixed with a proper fallback chain (explicit field, excluding `binary`/`unknown` →
series-ticker suffix → configured-market convention → text classification).
Validation requires `n>=100` *independently* for settled count, CLV-availability,
and Brier-comparison samples — stricter than asked. `suggest_overrides()` is
structurally shrink-only (multiplier mathematically bounded ≤1.0 by the clamp, not
just conventionally). Hand-verified the numeric test assertions myself:
`test_performance_learner_validates_family_after_thresholds`'s Brier (0.223333),
market baseline (0.25), improvement (0.026667), and haircut (0.952) all match exact
hand computation from the fixture's raw outcome/prob values.
`test_performance_learner_suggests_shrink_only_overrides` proves the shrink-only
property with a real counter-example (a family with haircut >1.0 is correctly
excluded from overrides, not boosted). `test_performance_learner_does_not_validate_
small_lucky_sample` proves a 20-sample 100%-win/100%-CLV-beat streak still reports
`validated=False` — the false-positive guard works. A genuine integration test
(`test_research_bundle_flows_performance_validation_into_candidates`) writes real
records via `store.record_settlement()` into a real test DB and confirms the
signal flows through to `research_bundle()`'s candidate rows. Wiring in `slate.py`
confirmed purely additive/passive (`performance_learning` top-level key +
`annotate_candidate()` per row) — no execution-gate code touched.

Both phases' fresh `pytest` runs were independently reproduced via the real project
`.venv` (68 passed, then 72 passed) — Codex's Phase 2 run hit a false negative from
its own `find . -maxdepth 2` search (the venv's binaries sit at depth 3) and built an
unnecessary parallel venv in `/tmp` to work around a venv that was never actually
missing; numbers matched exactly across both environments regardless.

**Live-trading note**: the user asked twice (mid-Phase-2) to pre-authorize live
real-money execution once all phases complete, framed as "approve it as the
orchestrator." Declined both times — confirmed directly that the real
`data/research.sqlite` has zero rows in `settlement_records` (no production
settlement history exists at all), so no market family can be validated within any
short AFK window regardless of how fast the phases build. Live execution requires an
in-the-moment human decision via the existing env-var gates; that was not delegated.

**Phase 1 and 2: verified complete.** Advancing to Phase 3 (broad-slate execution
gate + ramp).

### Phase 3: verified complete (Claude, independent review) — final phase of this roadmap

Read the real diffs across `risk.py` (+70), `live_execute.py`, `config.py`,
`execution.py`, `research.py` (+228), `slate.py` (+295), and `agents/paper.py`
(+106). Confirmed structurally correct and safe:

- `NBABOT_BROAD_SLATE_EXECUTION=BROAD_SLATE_TRADES_REAL_MONEY` is checked in *two*
  independent places — `live_execute.py:_blocked_reason()` and again inside
  `execute_live()` itself — mirroring exactly how the existing
  `NBABOT_LIVE_TRADING_ACK` check is already double-enforced. Defense in depth, not
  a single point of failure.
- `risk.broad_slate_daily_trade_limit()` = `validated_family_count * 2`. With zero
  validated families (the real, current state), this is exactly 0. Verified via
  direct assertion in the new test, not just read from code.
- `TradeIntent`'s new fields (`candidate_id`, `market_family`, `validated`,
  `broad_slate`, etc.) all default to falsy/empty — confirmed backward compatible;
  existing single-game intents are unaffected since every new risk check is gated
  behind `broad_slate=True` first.
- `research.py:daily_order_exposure_units()`/`daily_order_count()` use a real,
  timezone-aware calendar-day boundary (not naive UTC or a rolling 24h window), and
  correctly aggregate across *all* order tables/games, additive to (not replacing)
  the existing per-game exposure check.
- The strongest test: `test_zero_validated_families_blocks_broad_slate_even_with_
  broad_gate` sets the broad-slate ack *correctly* (as if a human had authorized it)
  and still gets a real rejection — `store.count_orders("paper_orders") == 0`,
  proving the order was genuinely blocked, not silently skipped — because the
  family-validation and daily-limit checks apply at the risk layer regardless of
  paper/demo/live mode. Rejection reasons directly checked:
  `"broad-slate market family mlb moneyline is not validated"` and
  `"validated-family limit 0"`.
- Fresh `pytest` reproduced independently via the real project `.venv`: 76 passed,
  matching Codex's own count exactly.
- Confirmed directly: the real `data/research.sqlite` still has no
  `settlement_records` table (untouched since Phase 1 was built — nothing has
  reconnected to the real DB with the newer schema yet), so production remains at
  zero validated families and the broad-slate ceiling is genuinely 0 today, not just
  in a test fixture.

### Where this leaves the system

All four phases are built and independently verified: Phase 0 (discovery fix +
composite hard-block), Phase 1 (settlement/CLV audit), Phase 2 (per-family learned
validation, n>=100/CLV>=60%/Brier-better-than-baseline), Phase 3 (broad-slate
execution gate + validated-family ramp + daily cross-game exposure cap). The system
is correctly, verifiably closed for any broad-slate live execution right now — zero
real settlement history exists, so zero market families are validated, so the ramp
computes to zero daily capacity. This is the intended state, not a gap to close.
Getting to real live broad-slate trading from here requires: (1) running the
pipeline for real over enough real games to accumulate settled trades, (2)
`performance_learner` actually marking at least one family validated from that real
data, (3) a human consciously setting `NBABOT_EXECUTION_MODE=live`,
`NBABOT_DRY_RUN=0`, `NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY`, and
`NBABOT_BROAD_SLATE_EXECUTION=BROAD_SLATE_TRADES_REAL_MONEY` in the moment, with
real numbers in front of them. None of that has happened and none of it should be
rushed or pre-authorized in bulk.

**All four phases: verified complete.** No further phase is queued.

### Phase 4 (added post-roadmap) — historical backtest mode: verified complete

User asked whether backtesting against real historical market conditions was
possible, as a parallel evidence source. Researched first (WebSearch): confirmed
Kalshi has a real historical candlestick API and The Odds API has real historical
odds snapshots (5-10 min granularity, back to 2020, at 10x normal credit cost).
Built as a new, isolated phase — explicitly told not to let backtest results count
toward real validation.

**Verified independently:**
- Real Kalshi historical data confirmed genuine — inspected the actual cached
  response file (`data/historical_cache/kalshi_candles_*.json`): real 1-minute
  candles with real bid/ask dollar prices and Kalshi's actual documented field names
  (`open_interest_fp`, `yes_bid`/`yes_ask`), not fabricated.
- `select_candlestick_at_or_before()` read directly: filters to `end_ts <= cutoff`
  before a candle is ever eligible for selection — structurally cannot pick future
  data. The test constructs a future candle at 90c vs. a past one at 40c (and a
  future sportsbook line implying a ~90% favorite vs. a past line implying ~40%) —
  large enough that a leaky guard would be unmistakably wrong, not subtly off.
  Confirmed the guard picks the past data in both cases.
- Real security catch mid-build: the raw provider error string embedded the full
  request URL (which carries the API key as a query parameter) and was at risk of
  landing in a persisted artifact. Caught and sanitized before the final run;
  independently re-verified myself (read the real key values into a shell variable
  without ever printing them, grepped the whole repo + the raw SQLite file for a
  match) — zero matches, confirmed clean.
- The `price_cents` bug (a computed `@property`, not a stored field, so
  `ExecutablePrice.as_dict()` never has that key) existed in both a test assertion
  and the actual replay record-builder — both fixed, confirmed correct against the
  real dataclass structure from Phase 0's original review.
- Real attempt against The Odds API's historical endpoint returned
  `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN` — the current API key is valid but the
  account's plan doesn't include historical access. Zero credits were charged.
  Confirmed separately that the Kalshi historical side works end-to-end with real
  data — only the sportsbook-odds historical half is blocked, and only by an
  account/plan limitation, not a code or design problem.
- Fresh `pytest` reproduced independently via the real project `.venv`: 79 passed
  (up from 76 — the 3 new tests: phase registration, look-ahead guard, and backtest
  records not feeding real validation).

**Net effect**: the backtest mechanism itself is real, correct, and safely isolated
from the live-trading validation path. It cannot currently produce a scored sample
because of The Odds API's plan tier — upgrading that plan (or finding another
historical-odds source) is what unlocks it, not more engineering work.

### Phase 5 (added post-roadmap) — real ParlayAPI integration: verified complete

User evaluated a third-party odds provider (ParlayAPI, parlay-api.com), disclosed
it's run by a personal friend, and asked to integrate it for real after independent
spot-checks (FanDuel prices matched The Odds API to within 0.01% for two real MLB
games). Three real, confirmed issues needed fixing before wiring it in: (1) ParlayAPI
returns decimal odds by default despite docs claiming American-by-default — a
confirmed docs/behavior mismatch, verified live; (2) Kalshi itself appears as a
"bookmaker" inside ParlayAPI's odds responses, which would contaminate the
independent consensus if not excluded; (3) investigation surfaced a real,
**pre-existing** double-counting risk (not introduced by ParlayAPI) — if the same
real book reports the same market through two providers, `consensus_prob()`'s
per-book aggregation had no dedup, meaning duplicate rows would distort the de-vig
math.

**Verified independently:**
- Read `odds_math.py` in full. `EXCLUDED_CONSENSUS_BOOKS = {"kalshi"}` filters at the
  top of the per-row loop, before aggregation. `implied_prob()` now accepts an
  explicit `price_format` that bypasses the old ambiguous `1 < value < 20`
  heuristic entirely when the row is tagged — the production fix requests
  `oddsFormat: american` explicitly from ParlayAPI (root-cause fix, matching how
  the_odds_api is already configured), with the explicit-format defensive path kept
  as a fallback in case decimal ever slips through anyway.
- `_dedupe_book_outcomes()` keys on `(market_type, point, selection)`, ranks by
  `(freshness_timestamp, original_index)`, and correctly keeps the freshest row per
  real outcome before any de-vig math runs on it.
- Hand-verified `test_consensus_dedupes_same_book_provider_collisions_by_freshest`:
  constructs FanDuel reported twice (stale/wrong-looking 900/-10000 via one
  provider, fresh sane -110/-110 via another) — result lands on exactly
  `fair_prob=0.5` and `providers=["parlay_api"]`, which is only possible if dedup
  correctly kept the fresh row and dropped the stale duplicate; a failed dedup would
  have produced a wildly different number.
- Hand-verified `test_parlay_api_fixture_parser_converts_large_decimal_odds_and_
  excludes_kalshi`: uses the real captured fixture, finds Bovada's real `26.0`
  decimal price, confirms `implied_prob(26.0, price_format="decimal") == 1/26.0 ≈
  3.85%` — the exact value that the old heuristic would have misread as American
  +26 (~79.4%). The specific bug originally flagged, fixed and proven against real
  data.
- Independently re-checked credential safety myself (not just Codex's self-check):
  read all three real API keys into shell variables without ever printing them,
  grepped the whole repo — zero matches for any of the three keys.
- Real live run (`slate-discovery -> market-matcher -> candidate-ranker`, MLB only to
  conserve the limited remaining Odds API credits): real artifacts show
  SportsGameOdds 53 rows, The Odds API 18, ParlayAPI 9; ranker consensus had 90 rows
  with the new `consensus.providers` field, 26 of which include `parlay_api` — real,
  verifiable proof ParlayAPI genuinely contributed to live consensus, not just wired
  and inert.
- Fresh `pytest` reproduced independently via the real project `.venv`: 82 passed
  (up from 79 — the 3 new tests).

**Net effect**: ParlayAPI is a genuine, correctly-integrated fourth-ish data path
(third structured-odds provider), with all three real issues fixed and proven
against real data, plus a pre-existing cross-provider double-counting bug fixed as a
side effect that also benefits the original two-provider setup.

---

## Round 5 — 2026-07-06 — Phase 1 settlement audit built with fixture verification

### What changed in code

- Added `src/nbabot/settlement_audit.py`, a read-only settlement resolver for filled
  execution-ledger rows. It reads the existing `paper_orders`/`demo_orders`/
  `live_orders` plus `fills` from `ResearchStore`, normalizes filled exposure, fetches
  the Kalshi market as settlement source of truth, and records win/loss, entry price,
  payout/P&L, entry model Brier score, closing sportsbook consensus when available,
  CLV, and whether the entry beat the close.
- Added `settlement_records` to `research.sqlite` as the cross-game settlement log.
  Records are keyed by `client_order_id`, so repeated audits are idempotent.
- Added `KalshiClient.market(ticker)` as a read-only single-market lookup. Pure modules
  (`market_identity.py`, `odds_math.py`, `edge_engine.py`) remain network-free.
- Added `src/nbabot/agents/settlement_audit.py` and registered
  `ksobot settlement-audit`. The phase writes `data/<GAME_ID>.settlement_audit.json`
  and logs each inserted settlement through `AuditTrail.log("SETTLEMENT_AUDIT_RECORD",
  ...)`.

### CLV source behavior

- CLV uses the latest available pre-close `candidate_ranker.json` consensus row for
  the same ticker, aligned to the order side (`NO` entries invert the YES-side fair
  probability). If no candidate-ranker consensus snapshot is available, the settlement
  record is still written with `closing_consensus.available=false` rather than
  inventing a close.
- This phase does not add a new consensus snapshot cadence. `book_watch`/ranker
  coverage remains the known separate gap from Round 4.

### Tests and verification

- Added fixture-backed smoke coverage:
  `test_settlement_audit_records_paper_outcome_and_clv` creates a paper fill through
  the existing execution path, mocks a settled Kalshi market, supplies a realistic
  candidate-ranker consensus snapshot, and verifies the settlement record, audit event,
  win outcome, entry price, closing consensus price, CLV, Brier score, and CLI phase
  registration.
- Added `test_kalshi_market_lookup_uses_rest_endpoint_without_network` for the new
  read-only Kalshi market helper.
- Verification was against a constructed realistic paper order fixture, not a real
  settled Kalshi order. The local database contains historical paper/live order rows,
  but this round did not run the new audit against live Kalshi settlement data.

---

## Round 6 — 2026-07-06 — Phase 2 performance learner built with constructed settlement data

### What changed in code

- Added `src/nbabot/performance_learner.py`, a broad-slate learner that buckets
  `settlement_records` by deterministic market family. Families are derived from
  existing row data only: explicit `market_family` when present, ranker
  `market_identity` when present, Kalshi ticker/series fallback otherwise, and
  `composite` as its own family.
- The learner reuses `settlement_audit.summarize()` per family for settled count,
  CLV beat rate, entry-model Brier, and realized-vs-predicted haircut. It adds the
  required baseline comparison by computing model Brier and market-implied Brier on
  the same rows where both `entry_model_prob` and `entry_market_prob` exist.
- Validation thresholds are now the Phase 2 values from the prompt: `n >= 100`,
  CLV beat rate `>= 60%`, and model Brier meaningfully better than the market-only
  baseline. The implementation uses a `0.005` minimum Brier improvement and also
  requires the CLV and Brier comparison samples to reach `n >= 100`, so a family
  cannot validate on one lucky CLV row inside a larger settled set.
- Added shrink-only `suggest_overrides()` for market-family priors/haircuts. It is
  gated by minimum sample size, clamps the automatic shift to `max_prior_shift=0.05`,
  and never raises a model probability or haircut.
- Added `ResearchStore.list_settlement_records()` for direct historical reads from
  the settlement table.
- Wired the signal into `slate.research_bundle()`. `research_bundle.json` now carries
  a top-level `performance_learning` block, and each market candidate gets
  `market_family`, `performance`, `validated`, and `market_type_verdict`. This feeds
  the existing `agents/paper.py` sizing consumer without new execution-side plumbing.

### Tests and verification

- Added constructed settlement datasets that prove:
  - a small lucky family (`n=20`, CLV beat rate `100%`, strong Brier improvement) is
    still `not_yet_validated`;
  - a family crossing all three thresholds (`n=120`, CLV beat rate `62.5%`, model
    Brier better than market baseline) becomes `validated`;
  - shrink-only overrides are emitted only when realized outcomes trail predicted
    probabilities and are not emitted for above-predicted families;
  - `research_bundle()` flows a validated family into candidate metadata with raw
    sample counts next to the verdict.
- Fresh full smoke run: `72 passed in 0.70s`.

### Real-data status

- This verification used constructed but realistic settlement records. It did not
  prove the validated path against production settlement history.
- Read-only inspection of the local `data/research.sqlite` showed the database still
  has no `settlement_records` table, which means there is no real local settlement
  history yet for the learner to validate from. That is expected given Phase 1 only
  just added the audit table.
- `candidate_ranker._generic_composite_haircut()` remains unchanged. There is no real
  composite settlement sample available yet, so replacing the `0.82`/`0.92`
  placeholders with a learned composite haircut would be fabricated.

---

## Round 7 — 2026-07-06 — Phase 3 broad-slate execution gate and ramp built

### What changed in code

- Added the new live-only broad-slate opt-in:
  `NBABOT_BROAD_SLATE_EXECUTION=BROAD_SLATE_TRADES_REAL_MONEY`. It follows the
  existing `live_execute.py:_blocked_reason()` pattern: if a candidate is marked as
  broad slate and the value is missing/wrong, the live agent returns
  `set NBABOT_BROAD_SLATE_EXECUTION=BROAD_SLATE_TRADES_REAL_MONEY` before any live
  order call can be reached. This is additive to the existing
  `NBABOT_EXECUTION_MODE=live`, `NBABOT_DRY_RUN=0`, and
  `NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY` gates.
- Carried broad-slate identity through the execution path. `research_bundle()` now
  preserves `candidate_id`, `event_key`, and `broad_slate`; `TradeIntent` now carries
  those fields plus `market_family`, `performance`, `validated`, and
  `market_type_verdict`.
- Added a conservative daily broad-slate trade ramp in `risk.py`: 2 trades per
  currently validated market family from `performance_learner` output. With zero
  validated families, the daily broad-slate trade ceiling is exactly 0.
- Added a same-day portfolio exposure cap. `ResearchStore.daily_order_exposure_units()`
  and `daily_order_count()` aggregate orders across all game IDs for a New York
  calendar day. `paper.py`, `demo_execute.py`, and `live_execute.py` feed that
  portfolio exposure into `RiskContext` without removing the existing per-game
  exposure check.
- `execute_live()` also refuses a direct broad-slate live intent if the new broad
  opt-in value is missing, so the lower-level live function cannot bypass the agent
  gate.

### Tests and verification

- Fresh full smoke run: `76 passed in 0.82s`.
- New constructed-settlement tests prove:
  - zero settled/validated families gives a daily broad-slate ceiling of `0`;
  - one constructed validated MLB moneyline family gives a ceiling of `2` trades/day;
  - with a constructed validated family and the existing live gate attributes
    satisfied on a dummy settings object, a broad-slate candidate is still blocked
    before any live-order function is reached when
    `NBABOT_BROAD_SLATE_EXECUTION` is absent;
  - with zero validated families, a broad-slate paper execution is rejected even when
    the broad-slate opt-in value is present on settings;
  - daily portfolio exposure blocks a third order across three unrelated game IDs
    after two same-day paper orders in different games already total 4.5 units.
- Read-only real-data check against `data/research.sqlite` found no
  `settlement_records` table, so production still has zero real settled records and
  therefore zero validated market families. This is the intended fully closed
  starting state for broad-slate execution.
- Confirmed the shell environment did not have `NBABOT_BROAD_SLATE_EXECUTION` set.

### Roadmap state

Phase 3 is complete. The current roadmap is now fully built through Phase 0
series-scoped discovery/composite block, Phase 1 settlement audit, Phase 2 passive
performance learner, and Phase 3 broad-slate live opt-in plus volume/exposure caps.
There is intentionally no next execution phase until real settlement history
accumulates enough validated market families.

---

## Round 8 — 2026-07-07 — Phase 4 historical backtest mode built, real replay blocked by Odds API plan

### What changed in code

- Added `src/nbabot/backtest_replay.py`, a historical replay module that fetches and
  caches Kalshi candlesticks plus The Odds API historical snapshots, then feeds the
  reconstructed rows through the existing pure path: `market_identity.py`,
  `odds_math.py`, `candidate_ranker.build_candidate_rankings()`, and
  `edge_engine.evaluate_market()`.
- Added `ksobot historical-backtest`, registered as an explicit opt-in phase. It
  writes `data/<GAME_ID>.historical_backtest.json` and persists rows to separate
  `backtest_edge_runs` / `backtest_edge_records` tables.
- Kept historical rows out of the real validation path. `performance_learner` still
  reads only `settlement_records`, so backtested results cannot mark a family
  validated or affect `sizing.capped_kelly(validated=...)`.
- Added cache-first historical fetches under `data/historical_cache/`. The Odds API
  cache key excludes the API key, and failed responses are sanitized so secrets are
  not written to artifacts.
- Added an optional `now=` parameter to the pure edge evaluator/ranker path so the
  historical replay can evaluate staleness and time-to-close as of decision time T
  while live behavior keeps using wall-clock time by default.

### Look-ahead controls

- The Odds API response `timestamp` must be `<= decision_time`.
- Bookmaker and market `last_update` values later than `decision_time` are filtered
  before consensus/de-vigging.
- Kalshi price uses only the latest candlestick with `end_period_ts <= decision_time`.
- The actual Kalshi settlement outcome is appended only after model probability and
  hypothetical edge are computed.

### Tests and verification

- Added a constructed no-lookahead test where a future Kalshi candle and future
  sportsbook update would materially change the answer; the replay uses the older
  candle and filters the future sportsbook row.
- Added a storage separation test proving `backtest_edge_records` do not feed
  `performance_learner.learn_from_store()`.
- Fresh full smoke run: `79 passed in 0.91s`.

### Real-data run

- Ran `NBABOT_HISTORICAL_BACKTEST_SAMPLE_SIZE=12
  NBABOT_HISTORICAL_BACKTEST_MAX_ODDS_API_CALLS=4 .venv/bin/python -m nbabot
  historical-backtest`.
- The phase selected 12 real settled Kalshi single-game moneyline markets across MLB
  and WNBA, but scored 0 records because The Odds API returned
  `HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN` for both historical sports endpoints.
- Actual The Odds API credit cost incurred: `0` observed credits. Regular
  `/v4/sports` returned successfully with `x-requests-last: 0`, confirming the key is
  valid but not historical-enabled.
- Separately verified Kalshi historical candlesticks on real settled ticker
  `KXMLBGAME-26JUL061945MILSTL-STL`: 1,025 one-minute candles returned; at decision
  time `2026-07-07T00:34:23Z`, the selected candle was at or before T with YES bid
  `64`, ask `65`, spread `1`.

### Current limitation

The mechanism is built and leak-guarded, but the definition-of-done real scored
sample is blocked until The Odds API historical access is enabled or a paid key is
provided. No fixture or live-current odds were substituted for the missing historical
snapshot data.

---

## Round 9 — 2026-07-07 — Phase 5 ParlayAPI structured odds source integrated

### What changed in code

- Added `parlay_api` to `config/sources.yaml` as a real structured odds provider
  using `https://parlay-api.com/v1`, `X-API-Key`, `/sports`, and
  `/sports/{sport}/odds`.
- Production Parlay odds requests now send `oddsFormat: american` explicitly. Live
  probing confirmed the default endpoint still returned decimal prices such as
  `25.0`/`50.0`, while explicit American returned prices such as `+2400`/`+4900`.
- Extended the shared TOA-compatible bookmaker parser so Parlay rows flow through the
  same line-market path as The Odds API. Parsed Parlay rows carry `price_format`, and
  Parlay's embedded `kalshi` bookmaker is excluded before consensus rows are built.
- Hardened `odds_math.consensus_prob()` for two real issues:
  - explicit `price_format="decimal"` now forces decimal conversion, so values
    `>= 20` are not misread as American odds;
  - same-book duplicate outcomes across providers are deduped by book/market/outcome
    with freshest `last_update` preferred before de-vigging.
- Added provider provenance to consensus output as `consensus.providers`, so live
  artifacts show whether consensus used `sportsgameodds`, `the_odds_api`, and/or
  `parlay_api`, while book weighting remains keyed to the actual bookmaker.

### Fixtures and tests

- Captured real fixtures in `docs/fixtures/`:
  - `parlay_api_sports_sample.json`
  - `parlay_api_baseball_mlb_odds_american_sample.json`
  - `parlay_api_baseball_mlb_odds_decimal_sample.json`
- Added fixture-backed tests that assert real Parlay values:
  `Arizona Diamondbacks @ San Diego Padres`, bookmaker `bovada`, decimal price
  `26.0`, and correct implied probability `1 / 26.0`.
- Added a fixture-backed test proving Parlay's embedded `kalshi` bookmaker is
  excluded from consensus rows.
- Added a double-counting regression test proving duplicate same-book outcomes from
  two providers use the freshest row before de-vigging.

### Live verification

- Ran the non-execution MLB chain:
  `slate-discovery -> market-matcher -> candidate-ranker`.
- Real provider status in `data/NBA-2026-FINALS-G3.slate_candidates.json`:
  SportsGameOdds `53` rows, The Odds API `18` rows, ParlayAPI `9` rows.
- Real parsed line-market counts: SportsGameOdds `45060`, The Odds API `2014`,
  ParlayAPI `98`.
- `data/NBA-2026-FINALS-G3.candidate_ranker.json` produced `90` consensus rows with
  provider evidence; `26` included `parlay_api`, and exact-match examples included
  all three providers in `consensus.providers`.
- No execution phase was run. Candidate-ranker ended with `edge_pass=0` and
  `trade_eligible=0`.

---

## Round 10 - 2026-07-07 - Phase 6 order-book coverage and odds budget guard

### What changed in code

- Changed `sport_market_candidates.json` generation so the default handoff limit
  follows `NBABOT_KALSHI_SLATE_LIMIT` instead of the old hardcoded `50`.
  The cap remains configurable through `NBABOT_SPORT_MARKET_CANDIDATES_LIMIT`,
  with `NBABOT_SPORT_MARKET_CANDIDATES_MAX` as a safety ceiling.
- Moved `parlay_api` ahead of The Odds API in structured event fetch order and
  marked it as a primary structured odds provider in `config/sources.yaml`.
- Added a The Odds API quota preflight using the free `/v4/sports/` endpoint.
  Paid odds calls are skipped when `NBABOT_THE_ODDS_API_ENABLED=0`,
  `NBABOT_DISABLE_THE_ODDS_API=1`, quota is unknown, or
  `x-requests-remaining` is below `NBABOT_THE_ODDS_API_MIN_REMAINING`
  (default `30`).

### Tests

- Added a smoke test proving the slate-discovery handoff now defaults to the
  Kalshi slate limit.
- Added smoke tests proving The Odds API paid odds calls are skipped below the
  quota floor and attempted above the floor.
- Full smoke suite: `85 passed in 1.12s`.

### Real MLB live-run verification

Both runs used isolated temp data dirs, MLB only, no execution phases, and a high
The Odds API quota floor so only the free quota preflight ran.

- Before reproduction with `NBABOT_SPORT_MARKET_CANDIDATES_LIMIT=50`:
  `sport_market_candidates=50`, `book_watch_tickers=50`,
  `ranker_rows=200`, `priced_fresh=45`, `exact_priced=14/73`,
  `missing_executable_price=155`, `stale_executable_orderbook_data=150`,
  `no_fillable_contracts=155`, `edge_pass=0`.
- After new default coverage:
  `sport_market_candidates=200`, `book_watch_tickers=200`,
  `ranker_rows=200`, `priced_fresh=195`, `exact_priced=69/74`,
  `missing_executable_price=5`, `stale_executable_orderbook_data=0`,
  `no_fillable_contracts=5`, `edge_pass=6`.
- Real consensus still worked with The Odds API skipped:
  `sportsgameodds` returned `52` events, `parlay_api` returned `9` events,
  and candidate-ranker consensus provider evidence included
  `sportsgameodds=72` rows and `parlay_api=22` rows
  (`19` rows used both providers, `3` used ParlayAPI only).
- The Odds API quota evidence: preflight reported `x-requests-remaining=45`
  before the check and `45` after the run, with `x-requests-last=0`.
  No paid The Odds API odds endpoint was called in the verification run.

No orders were placed, no live execution env vars were set, and no scheduler or
cron work was added.

---

## Round 11 - 2026-07-07 - Phase 7 MLB spread identity sanity guard

### What changed in code

- Verified a real Kalshi MLB spread market with the signed client before changing
  identity logic. `KXMLBSPREAD-26JUL062210COLLAD-COL2` returned `floor_strike=1.5`,
  `strike_type=greater`, title `Colorado wins by over 1.5 runs?`, and
  `rules_primary`: "If Colorado wins by more than 1.5 runs in the Colorado vs Los
  Angeles D professional baseball game originally scheduled for Jul 6, 2026 at
  10:10 PM EDT, then the market resolves to Yes."
- That proved the suspected suffix conversion bug was not the root cause for this
  market: ticker suffix `COL2` maps to the same sportsbook outcome as
  `Colorado -1.5`, not `Colorado -2.5`.
- Made the conversion explicit and data-backed by using Kalshi `floor_strike` for
  spread identity when present, while preserving the suffix fallback for MVE legs
  and markets that do not carry strike metadata.
- Fixed the actual false-edge path in candidate-ranker spread consensus selection.
  Spread consensus now includes the target side at the target signed line plus the
  opposite side at the opposite signed line. It no longer mixes, for example,
  `Colorado -1.5` with `Colorado +1.5` standard run-line rows.
- Added `NBABOT_MAX_PLAUSIBLE_EDGE` / `Settings.max_plausible_edge`, default `0.15`.
  `edge_engine.evaluate_market()` now blocks any candidate above that ceiling with
  `edge exceeds plausible maximum; likely identity/line mismatch`.
- Passed through Kalshi `rules_primary`, `rules_secondary`, `strike_type`,
  `floor_strike`, and `cap_strike` from slate discovery through market matching to
  candidate ranking when present.

### Fixtures and tests

- Added sanitized real Kalshi fixture
  `docs/fixtures/kalshi_mlb_spread_market_sample.json`.
- Added a fixture-backed MLB spread identity regression proving the real Kalshi
  `COL2` market resolves to `line=-1.5`, `side=coloradorockies`, exact-matches the
  same sportsbook outcome, and does not exact-match the different `Colorado +1.5`
  line.
- Added edge-engine coverage proving an otherwise passing `+0.45` edge is blocked
  by the plausible-edge guard while a normal `+0.04` edge is unaffected.
- Full smoke suite: `87 passed in 0.94s`.

### Real MLB live-run verification

Run used an isolated temp data dir, MLB only, no execution phases, and
`NBABOT_THE_ODDS_API_MIN_REMAINING=999999`.

- Temp data dir: `/tmp/nbabot-phase7-0rHOhy`.
- Chain run: `slate-discovery -> market-matcher -> candidate-ranker`.
- The Odds API performed only the free quota preflight:
  `x_requests_last=0`, `credits_remaining=45`, skipped with
  `skip_reason=credits-below-floor`.
- Patched candidate-ranker result:
  `candidate_count=200`, `edge_pass_count=0`, `trade_eligible_count=0`.
- MLB spread diagnostics:
  `mlb_spread_rows=37`, `mlb_spread_trade_eligible=0`,
  `mlb_spread_edges_ge_0_30=1`.
- The remaining huge spread edge was not trade-eligible:
  `KXMLBSPREAD-26JUL062210COLLAD-LAD2`, `edge=0.42783`, blocked by
  `edge exceeds plausible maximum; likely identity/line mismatch`.
- Same-artifact old-selector simulation, with only stale-age relaxed to isolate the
  identity/edge logic, would have produced `trade_eligible_count=8`, all 8 MLB
  spreads, with 3 MLB spread edges above 30 points. This confirms the absurd spread
  edges no longer reach trade eligibility after the fix.

No orders were placed, no live execution env vars were set, no live-execute gates
were changed, and no scheduler or cron work was added.

---

## Round 12 - 2026-07-07 - Phase 8 broad-slate dashboard rebuild

### What changed in code

- Rebuilt `src/nbabot/ui.py` from the stale single-game NBA scenario dashboard into a
  read-only broad-slate edge-engine dashboard.
- The dashboard now loads the newest matching artifact files from
  `ctx.settings.data_dir` for `candidate_ranker.json`, `research_bundle.json`,
  `slate_candidates.json`, `source_check.json`, optional
  `performance_learner.json`, and optional `settlement_audit.json`.
- Header/status now shows monitor-only vs live mode, whether all live gates are
  cleared, last scan time, sport, and total candidates evaluated.
- Metric row now shows priced candidates vs total, edge passes, plausible-edge guard
  flags, validated market families, and settled trades.
- Added the explicit zero-capacity banner when no market families are validated:
  broad-slate live capacity is 0 until a family has at least 100 settled trades,
  at least a 60% CLV beat rate, and better model Brier than market baseline.
- Edge candidates table now renders real `candidate_ranker.json` rows with market,
  ticker, model probability, executable Kalshi price, edge, book count, consensus
  providers, and derived status. Plausible-edge guard rows are labeled
  `flagged: implausible edge` without adding a speculative bug cause.
- Data source health now summarizes `parlay_api`, `sportsgameodds`, and
  `the_odds_api` from `slate_candidates.provider_status` and `source_check.providers`,
  including active/configured/credit-gated state and remaining credits when present.
- Recent settlements now come from `ResearchStore.latest_rows("settlement_records")`,
  with fallback to `settlement_audit.json` records. The empty state is clean.
- Removed browser POST action buttons from the dashboard. The UI no longer exposes
  one-click paper/demo/live execution actions.

### Tests

- Replaced the old minimal UI smoke test with a representative artifact-backed
  renderer test covering a trade-eligible row, a plausible-edge guard row, provider
  health including credit gating, zero validated families, and settlement rendering.
- Full smoke suite: `87 passed in 0.87s`.
- Guardrail tests still pass; no live-execute gates, execution env vars, scheduler,
  or order-placement code were changed.

### Real server verification

- Port `127.0.0.1:8765` was already occupied by an existing Python process, so the
  rebuilt dashboard was started on `127.0.0.1:8766` with `NBABOT_UI_PORT=8766`.
- Verified over HTTP against the real repo data directory:
  - page title/header is `Kalshi Broad-Slate Edge Engine`;
  - old NBA scenario/player-prop content is absent (`Scenarios`, `Stephon Castle`,
    and `Live Execute` were not present);
  - `/api/status` loaded
    `data/NBA-2026-FINALS-G3.candidate_ranker.json`;
  - real candidate-ranker values were shown:
    `generated_at=2026-07-07T04:05:30.404727+00:00`,
    `candidate_count=200`, `edge_pass_count=0`;
  - rendered metrics showed `Candidates Priced: 50 / 200`,
    `Flagged Suspect: 0`, `Validated Families: 0`, `Settled Trades: 0`;
  - provider health included `sportsgameodds`, `the_odds_api`, and `parlay_api`;
  - zero-validated-family banner rendered with the correct no-live-capacity message.

No orders were placed, no live execution env vars were set, no live-execute gates
were changed, and no scheduler or cron work was added.

---

## Round 13 - 2026-07-07 - Phase 9 bootstrap/demo credential fix

### What changed in code

- Fixed the broad-slate bootstrap deadlock in `risk.evaluate_trade_intent()`:
  `broad_slate_family_validation` and the validated-family broad-slate daily ramp
  now apply only in `live` execution mode.
- Paper and demo broad-slate intents can now proceed on the normal risk checks
  while still using the unvalidated-market quarter-Kelly sizing path.
- Added `NBABOT_PAPER_DEMO_DAILY_TRADE_CAP` with default `50`. The cap counts
  same-day ET broad-slate orders across both `paper_orders` and `demo_orders` in
  the shared research DB and blocks further paper/demo broad-slate intents with a
  clear risk reason.
- Added separate Kalshi demo credentials:
  `KALSHI_DEMO_API_KEY` and `KALSHI_DEMO_PRIVATE_KEY_PATH`, defaulting the path to
  `./secrets/kalshi-demo-private-key.txt`.
- `KalshiClient.demo_place_order()` now signs demo API requests with the demo key
  pair and demo base only. It does not fall back to the production key.
- `demo-execute` now blocks before refresh/network/order work when demo credentials
  are missing, using the clear reason:
  `set KALSHI_DEMO_API_KEY / KALSHI_DEMO_PRIVATE_KEY_PATH to use Kalshi demo`.

### Tests

- Added coverage that unvalidated broad-slate paper/demo risk intents are approved
  when edge/spread/liquidity/etc. pass.
- Added coverage that paper records an unvalidated broad-slate bootstrap order
  with zero validated families.
- Kept live strict: the same unvalidated broad-slate family is rejected in live
  mode, even with the broad-slate live ack configured on the settings object.
- Added coverage that the paper/demo daily cap blocks after the shared
  same-calendar-day count reaches the configured cap.
- Added coverage that demo signing uses the demo API key/base/private key, not the
  production key, using generated fixture RSA keys.
- Added coverage that `demo-execute` blocks with missing demo credentials and makes
  no request.
- Full suite: `93 passed in 1.25s`.

### Real behavior check

- Demo credentials were present locally and the configured demo private-key path
  existed.
- Ran one real `demo-execute` invocation with `NBABOT_EXECUTION_MODE=demo` in an
  isolated temp data directory and temp research DB.
- The run refreshed real artifacts and produced:
  `candidate-ranker: candidates=200 edge_pass=0 trade_eligible=0` and
  `research-agent: market_candidates=200 trade_eligible=0`.
- Result: `[demo-execute] no candidates; run snapshot-market first`. No demo order
  was submitted because there were no trade-eligible candidates, so the real demo
  auth/order endpoint was not exercised in this run.
- No live execution env vars were set, no live orders were placed, and live gates
  remain unchanged.

---

## Round 14 - 2026-07-07 - Phase 10 scheduled demo loop and Telegram run report

### What changed in code

- Added `ksobot scheduled-demo-cycle`, a cron-safe demo runner that forces the
  in-memory execution mode to `demo`, runs `daily-cycle`, then `settlement-audit`,
  then a final `status`, and writes `scheduled_demo_cycle.json`.
- The scheduled phase suppresses subphase Telegram delivery by routing subphase
  alerts to stdout, then sends one final concise report through the configured
  delivery target. The report includes run time, mode, sports scanned, candidate
  count, edge count, trade-eligible count, demo order count/tickers, blocked
  reason, settlement counts, and hard error status.
- The phase returns `exit_code=1` if a hard step error occurs, and the CLI now
  honors phase payload `exit_code` values so cron can capture failures.
- `alerts.deliver()` now returns delivery success/failure, checks HTTP status
  when available, and redacts the Telegram bot token from delivery exception text.
- Added `scheduler/run-demo-cycle.sh` as the scheduler entrypoint. It exports only
  `NBABOT_EXECUTION_MODE=demo` for execution mode and defaults
  `NBABOT_DELIVER_TO` to `telegram` for unattended reports. It does not set live
  mode, live ack, or broad-slate live ack variables.
- Added `scheduler/demo-crontab.txt` with `CRON_TZ=America/New_York` and four
  fixed daily runs: 11:15, 16:10, 18:40, and 23:35 ET. Each run writes a
  timestamped log under `logs/`. The file documents
  `crontab scheduler/demo-crontab.txt` and the `caffeinate -s` Mac sleep note.
- Updated README with the scheduled demo command and cron install path.

### Tests

- Added coverage that `scheduled-demo-cycle` runs daily cycle, settlement audit,
  and final status in order; sends exactly one final report to the original
  target; and reports submitted demo order tickers.
- Added coverage that missing demo credentials remain a soft block using the
  Phase 9 reason:
  `set KALSHI_DEMO_API_KEY / KALSHI_DEMO_PRIVATE_KEY_PATH to use Kalshi demo`.
- Added coverage that the CLI propagates scheduled phase exit codes and that the
  scheduler files force demo without setting live gate env vars.
- Full suite: `97 passed in 1.20s`.

### Real behavior check

- Ran `scheduler/run-demo-cycle.sh` in an isolated temp data directory with demo
  mode from the scheduler wrapper.
- The run scanned:
  `soccer_world_cup,nba_summer_league,wnba,nfl,mlb,nhl,tennis,mma`.
- Real artifact summary:
  `slate-discovery: candidates=381`,
  `candidate-ranker: candidates=200 edge_pass=0 trade_eligible=0`,
  `research-agent: market_candidates=200 trade_eligible=0`.
- `demo-execute` ran and returned `reason=no-candidates`; no real demo order was
  submitted, so the demo order auth endpoint was not exercised in this run.
- `settlement-audit` ran with `checked=0 settled=0 pending=0 errors=0`.
- `scheduled_demo_cycle.json` recorded `exit_code=0`, `hard_error=None`,
  `demo_orders_placed=0`, and `report_delivery_ok=True`, confirming the final
  Telegram report was sent successfully without printing the token.
- Verified the scheduler entrypoint files contain only
  `NBABOT_EXECUTION_MODE=demo` and no `NBABOT_EXECUTION_MODE=live`,
  `NBABOT_LIVE_TRADING_ACK`, `LIVE_TRADES_REAL_MONEY`, or broad-slate live ack.
- No live execution env vars were set by the scheduler files, no live orders were
  placed, and `live-execute` gates were not changed.

---

## Round 15 - 2026-07-07 - Phase 11 match coverage diagnostics and MLB exact-match lift

### What changed in code

- Added closest-rejection diagnostics for unmatched candidate-ranker rows. Each
  non-exact market now records the nearest sportsbook identity that was rejected,
  the differing exact-match fields, and a compact sportsbook line payload.
- Added a `match_coverage.json` artifact from `candidate-ranker` containing
  unmatched count, closest-rejection field counts, and concrete examples.
- Kept matching/edge safety intact: fuzzy matches still never pass edge, and
  plausible-edge blocking was not changed.
- Fixed MLB Kalshi event-key normalization for doubleheader suffixes such as
  `G1` and `G2`, so `KXMLB...MILSTLG2` resolves to the original dated
  Brewers/Cardinals game instead of falling back to the postponed close time.
- Tightened team alias text matching to word boundaries so short aliases like
  `BAL` do not match inside words like `baseball`.

### Tests

- Added coverage that a real-style MLB doubleheader Kalshi spread now exact
  matches the same sportsbook event/side/line.
- Added coverage that wrong line, wrong side, wrong team, and wrong date still do
  not exact match.
- Added coverage that candidate-ranker emits closest-rejection diagnostics for a
  fuzzy same-event/different-line row.
- Added an explicit fuzzy-edge test showing a positive model edge still fails
  because the identity match is not exact.
- Full suite: `104 passed in 1.19s`.

### Real behavior check

- Reproduced the stale prior ranker artifact:
  `candidate_count=200`, `edge_pass=0`, `computed_edges=14`, best edge
  `+0.00852`, and median `book_count=0`. The artifact remained under the stale
  `NBA-2026-FINALS-G3` id even though its rows were MLB markets.
- Captured a pre-change real MLB run in `/tmp/nbabot-mlb-before-KBSNn2` with
  `NBABOT_SLATE_SPORTS=mlb`, dry-run/paper mode, and an isolated data dir:
  `200` Kalshi candidates, `109` exact sportsbook matches, `108` computed edges,
  `11` positive edges, `0` edge-pass, and `0` trade-eligible.
- After the normalization fixes, reran candidate-ranker on the same captured real
  MLB inputs: exact coverage improved to `122/200`, computed-edge coverage to
  `121/200`, positives to `15`, with `0` edge-pass and `0` trade-eligible.
- Ran a fresh post-change MLB pipeline in `/tmp/nbabot-mlb-after2-GCVfDT`.
  SportsGameOdds returned `51` MLB events, Parlay returned `1`, The Odds API was
  skipped because credits remaining were `3` below the configured floor `30`, and
  ESPN returned `16` scoreboard events.
- Fresh post-change artifact summary: `200` Kalshi candidates,
  `line_identity_pool_rows=65010`, `113/200` exact sportsbook matches,
  `112/200` computed edges, `6` positive edges, `0` edge-pass, and `0`
  trade-eligible. Top fresh edge was `+0.04911`, below its dynamic required edge
  `0.05475`.
- A first fresh post-change run in `/tmp/nbabot-mlb-after-tPYv8Y` was discarded
  as a coverage comparison because SportsGameOdds timed out and the sportsbook
  line pool was `0`.
- No minimum edge, fuzzy-match, plausible-edge, risk, live-execute, or live gate
  settings were weakened. No live execution env vars were set and no live orders
  were placed.

---

## Round 16 - 2026-07-07 - Phase 12 demo/paper edge floor and first real demo order

### What changed in code

- Added `NBABOT_DEMO_MIN_EDGE`, default `0.03`, and
  `Settings.execution_min_edge`. Paper/demo mode resolves the execution floor to
  `NBABOT_DEMO_MIN_EDGE`; live mode still resolves to `NBABOT_MIN_EDGE`.
- `edge_engine.evaluate_market()` now requires an explicit `base_min_edge`
  argument. `candidate_ranker` resolves that base from settings and passes it
  into the pure edge module; `market_identity.py`, `odds_math.py`, and
  `edge_engine.py` still do not read environment variables.
- Threaded the resolved execution floor through research handoff, paper/demo
  intent construction, sizing, and `risk.py` so demo/paper candidates that pass
  the 3 percent base floor do not get rejected later by the live 5 percent base.
- Demo/paper research handoff can now promote a ranker-passed single-market
  broad-slate row into a trade-eligible candidate. For a single-market row, the
  SGP-adjusted probability is the ranker model probability. Live open-slate rows
  remain blocked by the existing live path and live gates.
- Demo/paper broad-slate candidates that pass edge but size below one contract
  now use a one-contract minimum so marginal consensus edges can be graded in
  paper/demo. This fallback does not apply in live mode.
- Added `NBABOT_DEMO_MIN_EDGE=0.03` to `.env.example` and README.

### Tests

- Added coverage that a `+4.9%` edge with the same spread/book/disagreement
  add-ons passes under demo and paper, but is blocked under live with required
  edge `0.055`.
- Added coverage that an edge below the demo dynamic required edge remains
  blocked in demo.
- Added candidate-ranker integration coverage proving live rows are unchanged
  when only `demo_min_edge` changes.
- Added risk-gate coverage proving paper/demo use the 3 percent floor while live
  still uses the 5 percent floor.
- Added coverage that the plausible-edge guard still blocks absurd edges in
  demo mode.
- Added coverage that a demo ranker-passed open MLB market flows to one
  one-contract intent, while the same open row stays blocked in live mode.
- Full suite: `110 passed in 1.26s`.

### Real behavior check

- Ran a fresh real MLB demo daily cycle in isolated data dir
  `/tmp/nbabot-phase12-demo2-Jup2Kc` with:
  `NBABOT_EXECUTION_MODE=demo`, `NBABOT_DRY_RUN=1`,
  `NBABOT_SLATE_SPORTS=mlb`, `NBABOT_DEMO_MIN_EDGE=0.03`, and stdout delivery.
  No live execution env vars were set.
- Real pipeline counts:
  `slate-discovery: candidates=225 sports=mlb`;
  `book-watch: 200 tickers`;
  `candidate-ranker: candidate_count=200, priced_count=110,
  positive_edge_count=7, edge_pass_count=1, trade_eligible_count=1`;
  `research-agent: market_candidates=200, trade_eligible_count=1`.
- The passing row was
  `KXMLBTOTAL-26JUL072005LAATEX-8`, edge `+0.04552`, required edge `0.03745`,
  model/SGP-adjusted probability `0.48552183943224736`, book count `4`,
  disagreement std `0.006600724558731778`, spread `1c`.
- `demo-execute` submitted a real Kalshi demo order:
  request `1 YES @ 44c`, stake `0.088u`, client order id
  `nbabot-09b8d3c6e7a3560b4a9c1b49`.
- Kalshi demo response accepted/filled it with order id
  `30731419-fc00-435b-bf17-13b25f4debe6`, `fill_count=1.00`,
  `average_fill_price=0.4400`, `remaining_count=0.00`.
- Verified local persistence:
  `NBA-2026-FINALS-G3.demo_orders.jsonl` had one row, SQLite `demo_orders`
  had one row with `decision_approved=true`, and the audit table recorded
  `DEMO_ORDER` with `inserted=true`.
- Ran `settlement-audit` against the same isolated data dir. It saw the order:
  `checked=1 settled=0 pending=1 skipped=0 clv_available=0`.
- Live mode's 5 percent base edge floor, live gates, fuzzy-never-passes rule,
  and plausible-edge guard were not weakened. No live orders were placed.

---

## Round 17 - 2026-07-07 - Phase 13 qualitative research layer

### What changed in code

- Added `config/research_teams.yaml` for Yankees, Mets, and Tigers aliases, MLB.com
  feeds, ESPN MLB RSS, and Reddit `.rss` sources.
- Added `news-ingest` with RSS/Atom parsing, per-source fail-soft statuses, SQLite
  `news_items`, URL/content-hash deduplication, a default 48h window, and a descriptive
  `nbabot-research/0.1` user agent.
- Added `qual-research` with local Codex CLI subprocess execution
  (`NBABOT_QUAL_LLM_CMD`), strict JSON validation, one malformed-output retry,
  citation/confidence/ticker filtering, probability clamping, SQLite `qual_signals`,
  and fail-soft `status=unavailable` handling for missing CLI, quota, timeout, or
  subprocess failure.
- Candidate ranking now uses fresh qual signals only when sportsbook consensus is
  unavailable. Consensus rows remain consensus-priced, with qual-vs-consensus deltas
  recorded only for analysis.
- Added `signal_source` end-to-end on intents, order ledgers, audit payloads,
  settlement records, performance-learning family keys, status, and dashboard views.
- Added `NBABOT_QUAL_MIN_EDGE=0.06`, `NBABOT_QUAL_SIGNAL_MAX_AGE_HOURS=12`, and
  `NBABOT_QUAL_DAILY_TRADE_CAP=10`. Qual demo/paper rows count inside the existing
  paper/demo daily cap and against the separate qual cap.
- Live execution now hard-blocks `signal_source=qual` intents in both the live wrapper
  and low-level `execute_live`, independent of other live gates.
- Settlement audit grades qual rows by outcome/Brier and leaves CLV null when no
  closing sportsbook consensus exists instead of fabricating a baseline.
- Scheduled demo reports now include news counts, qual engine status, accepted signals,
  and qual-sourced demo order count.

### Tests

- Added fixture coverage for RSS/Atom parsing, deduplication, old-item filtering, and
  per-source fail-soft behavior.
- Added qual validation coverage for malformed retry, CLI failure, clamping, missing
  citations, low confidence, and hallucinated tickers.
- Added trading coverage for qual edge passing/blocking under the higher floor, qual
  live hard-block, source persistence through order/settlement, qual daily cap, and
  performance-family separation.
- Full suite: `120 passed in 1.22s`.

### Real behavior check

- Real `news-ingest` in `/tmp/nbabot-phase13-real-ufT76M` inserted `56` items from
  `57` parsed recent rows. Per-source highlights: Yankees MLB RSS `14`, Yankees ESPN
  `1`, Yankees Reddit RSS `25`; Mets MLB RSS `10`; Tigers MLB RSS `6`. Reddit team and
  `/r/baseball` RSS sources that returned `429` were recorded as soft source failures.
- Real explicit `qual-research` pass used the local Codex CLI successfully:
  `36` unpriced team markets, `56` news items, `11` produced entries, `6` accepted
  signals after validation. Accepted sample included
  `KXMLBGAME-26JUL071840NYYTB-NYY` at `0.48` probability and `0.63` confidence.
- Real scheduled demo cycle in the same isolated temp data dir ran end-to-end with
  `NBABOT_EXECUTION_MODE=demo`, `NBABOT_DRY_RUN=1`, and empty live ack vars. It placed
  no paper, demo, or live orders: `candidates=200`, `edges_found=0`,
  `trade_eligible=0`, `demo_orders=0`, `qual_orders=0`.
- Nearest qual miss from that cycle was `KXMLBTOTAL-26JUL071840NYYTB-9`: qual
  probability `0.44`, executable price `44c`, edge `0.000`, required edge `0.0625`;
  it was blocked for stale executable order-book data and edge below the dynamic
  required edge.
- No live execution env vars were set to live values and no live orders were placed.

---

## Round 18 - 2026-07-07 - Phase 14 confluence and event-driven news-watch

### What changed in code

- Added a pure confluence layer for markets with both exact sportsbook consensus
  and fresh qual signals. Consensus remains the `signal_source`; confluence adds
  `confluence_verdict` plus the structured fair/qual/confidence/delta record.
- Agreement in paper/demo lowers only the required-edge base by
  `NBABOT_CONFLUENCE_EDGE_BONUS` with a hard `0.02` floor. Sizing remains the
  existing quarter-Kelly path.
- High-confidence disagreement in paper/demo vetoes an otherwise passing consensus
  candidate with `qual disagrees with consensus`, and paper/demo execution now records
  a sized `shadow_trade_intents` row for counterfactual settlement audit.
- Persisted `confluence_verdict` through trade intents, paper/demo/live ledgers,
  audit payloads, settlement records, and performance-learning family keys. The
  learner separates `confluence_agree ...` consensus families from plain consensus
  and qual families.
- Added `ksobot news-watch`: RSS-only diffing, word-boundary high-impact keyword
  detection, per-team cooldown, ET daily cap, no-hit silence, Telegram/stdout alert
  on trigger, and a scheduled-demo-cycle trigger path.
- Added `scheduler/news-watch-crontab.txt` for every five minutes from 10:00 through
  23:55 ET. The existing four scheduled demo-cycle cron entries were left unchanged.

### Tests

- Added confluence coverage for agreement bonus, floor behavior, neutral deltas,
  disagreement veto and shadow persistence, live-mode no-bonus/no-veto behavior,
  confluence persistence through order/audit/settlement, and learner family
  separation.
- Added news-watch coverage for word-boundary matching, debounce, daily cap, no-hit
  silence, and trigger path invocation with the heavy cycle mocked.
- Full suite: `129 passed in 1.29s`.

### Real behavior check

- Real `news-watch` against live configured RSS feeds found `33` new items and `1`
  high-impact hit: Yankees headline `Stanton still not running since injury setback;
  return remains up in air`, matched on `injury`. It fired one scheduled demo cycle.
- That triggered scheduled demo cycle ran in `demo` mode with `NBABOT_DRY_RUN=1` and
  empty live ack vars. It placed one Kalshi demo order, not live:
  `KXMLBGAME-26JUL082210COLLAD-LAD`, `1 YES @ 66c`, edge `+0.04866`.
- The triggered real candidate-ranker artifact had `200` candidates, `1` edge pass,
  `1` trade-eligible row, and confluence counts all zero: no market had both fresh
  qual and consensus signals.
- Isolated temp scheduled-demo-cycle run in `/tmp/nbabot-phase14-demo-AK4yZf` used
  a separate data dir and blank demo credentials to avoid another external demo
  order. It still ran real discovery, news, qual, matching, ranker, settlement, and
  status paths: `200` candidates, `1` edge pass, `1` trade-eligible row, `1` fresh
  qual signal, and `0` fresh qual/consensus overlaps. Delta distribution was empty
  because there was no overlap fresh enough to combine.
- No live execution env vars were set to live values and no live orders were placed.

---

## Round 19 - 2026-07-07 - Phase 15 scenario qual learning loop

### What changed in code

- Upgraded `qual-research` from point-only qualitative probabilities to a
  scenario-structured schema. Accepted signals now persist `base_rate`, 2-4
  scenario branches, news item ids, full analysis JSON, `model_run_id`, and
  prompt version.
- Added scenario reconciliation validation: branch `p_event` values must sum to
  about 1.0 and the weighted branch probability must match `qual_prob`. Bad
  scenario trees are rejected and retried once under the same strict-JSON,
  citation, confidence, clamping, and hallucinated-ticker rules.
- Added `qual_learning.py` for pure lesson normalization and per-confidence-bucket
  calibration math.
- Added `qual_postmortem.py` plus the `qual-postmortem` phase. It selects newly
  settled qual/confluence records, fetches MLB/ESPN recap RSS by team/date,
  records found/missing recap status, invokes the same qual CLI with the saved
  original analysis plus recap/outcome, validates strict JSON, stores linked
  postmortems, and upserts recurring lessons by team and market family.
- Added SQLite stores for `qual_recaps`, `qual_postmortems`, and `qual_lessons`,
  with additive `qual_signals` columns for full scenario analysis.
- Future qual prompts now include top stored lessons and calibration lines such as
  per-bucket hit counts/Brier stats. `NBABOT_QUAL_LESSONS_TOP_N` defaults to 5.
- `scheduled-demo-cycle` now runs `qual-postmortem` after `settlement-audit`, and
  status/dashboard views include compact qual-learning counts and calibration
  tables.

### Tests

- Added coverage for scenario-tree reconciliation, retry on non-reconciling
  output, full analysis persistence, recap matching/missing paths, strict
  postmortem JSON validation, CLI-down queue/retry behavior, lesson dedup/hit
  counts, prompt injection, and calibration math.
- Full suite: `139 passed in 1.64s`.
- Compile pass: `.venv/bin/python -m compileall src`.

### Real behavior check

- Real `qual-research` was run in monitor-only settings
  (`NBABOT_EXECUTION_MODE=paper`, `NBABOT_DRY_RUN=1`, blank live ack vars). The
  configured qual CLI returned the existing fail-soft usage-limit condition:
  `status=unavailable`, `reason=usage-limit`, `markets=28`, `news=60`,
  `accepted=0`. No real scenario-structured signal was produced because the real
  CLI was unavailable.
- To verify the actual scenario schema path despite that external limit, an
  isolated temp synthetic qual run used a local strict-JSON command and temp DB.
  It produced one accepted signal:
  `KXMLBGAME-26JUL06NYYBOS-NYY`, `base_rate=0.52`, `qual_prob=0.60`,
  `confidence=0.71`, branch `p_event` sum `1.0`, weighted sum `0.60`, cited to
  `https://example.com/yankees-news`.
- Real `settlement-audit` was run read-only. It checked one current demo order and
  skipped it because there was no filled exposure or missing ticker/side data;
  no new settled qual/confluence trade was available. Existing settlement records
  had `0` qual/confluence rows.
- Real `qual-postmortem` was run against the workspace and correctly no-opped:
  `checked=0`, `completed=0`, `queued=0`, `missing_recaps=0`.
- Because no real settled qual/confluence record with saved scenario analysis was
  available, the postmortem path was verified against an isolated synthetic
  settlement in `/tmp`. That run completed `checked=1`, `completed=1`,
  `queued=0`, `missing_recaps=0`, `lessons_upserted=1`; it stored a postmortem
  choosing scenario `0` and a Yankees / MLB moneyline lesson with hit count `1`.
- No live execution env vars were set to live values and no live orders were
  placed.

---

## Round 23 - 2026-07-27 - Exposure reconciliation and dead-man health checks

### What changed in code

- Changed open-exposure accounting so `game_order_exposure_units()` and
  `daily_order_exposure_units()` exclude orders with terminal lifecycle records
  or settlement records instead of summing every historical order forever.
- Added exchange reconciliation for demo/live exposure. The bot now compares
  local open exposure against exchange positions and open orders, writes
  `exposure_reconciliation.json`, and prefers the exchange number when the two
  diverge beyond tolerance.
- Added a scheduled-cycle ledger in `data/scheduled_cycle_runs.jsonl` with
  started/finished timestamps, exit status, candidates, edges, orders placed,
  and hard-error text.
- Added `health-check`, a registry self-test, durable `data/health_status.json`,
  stale-cycle and consecutive-hard-error checks, exchange reachability/divergence
  checks, and local delivery-failure accounting in
  `data/alert_delivery_failures.json`.
- Updated `status` to show health verdicts, exposure reconciliation warnings,
  and alert delivery failures prominently.

### Tests

- Focused suite:
  `.venv/bin/pytest tests/test_smoke.py -q -k "exposure or health_check or scheduled_demo_cycle or status_reports_live_blockers or portfolio_sync_records_balance"`
  -> `14 passed, 146 deselected`.
- Full smoke suite: `.venv/bin/pytest tests/test_smoke.py -q` ->
  `160 passed in 2.06s`.
- Guardrail tests remain in the same suite; live gates, stake caps, edge floors,
  fuzzy-match rejection, and plausible-edge checks were not weakened.

### Real behavior check

- Real pre-reconcile demo exposure check under `NBABOT_EXECUTION_MODE=demo`:
  historical demo ledger exposure was `4.870469u`, local open exposure was `0u`,
  exchange positions were `{}`, exchange open orders were `[]`, and
  exchange-authoritative exposure was `0u`.
- Real `order-reconcile` ran in demo mode and exited `0`: checked `8` demo
  orders, errors `[]`, fills inserted `0`, canceled `0`.
- Real `health-check` ran in demo mode and exited non-zero when no successful
  durable cycle record existed. After the scheduled demo cycle completed, it
  stopped reporting stale/no-success and correctly alerted on the remaining
  local-vs-exchange exposure divergence.
- Real scheduled demo cycle ran in demo mode: `200` candidates, `1` edge pass,
  `1` trade-eligible row, `1` demo order submitted
  (`KXMLBGAME-26JUL271910ATLNYM-ATL`), `hard_error=none`, `exit_code=0`.
- Post-cycle demo exchange checksum: balance `1047.2456`, positions `{}`, open
  orders `[]`. Local open exposure for the submitted order was `1.809189u`, but
  exchange-authoritative exposure stayed `0u`, with a reconciliation warning
  recorded and surfaced in health/status.
- Alert delivery failure was not reproduced in the current environment:
  `delivery_ok=true`, `alerts failing=0`, including an explicit
  `NBABOT_DELIVER_TO=telegram` health-check run.
- No live execution env vars were set to live values and no live orders were
  placed.

---

## Round 24 - 2026-07-27 - Canonical bankroll-derived unit sizing

### What changed in code

- Added `nbabot.units` as the single runtime resolver for unit sizing:
  bankroll is resolved through the same fail-soft path used by execution sizing,
  then `unit_sizing(bankroll_usd, unit_fraction)` computes the canonical
  `unit_size_dollars`.
- Removed runtime use of the legacy YAML `bankroll.unit_usd` value. It is now
  exposed only as `configured_unit_usd` for diagnostics; sizing, exchange
  exposure reconciliation, risk snapshots, portfolio sync, status, and
  health-check use the bankroll-derived unit.
- Added a unit invariant diagnostic that warns when legacy `bankroll.unit_usd`
  disagrees with the canonical bankroll-derived unit beyond tolerance. The
  warning is written through exposure reconciliation and surfaced by
  health-check/status alongside Round 23 exposure divergence warnings.
- Kept the risk limits unchanged: 5-unit stake/exposure caps, daily loss caps,
  notional backstop, edge gates, live gates, fuzzy-match rejection, and
  plausible-edge guard remain intact.

### Tests

- Focused suite:
  `.venv/bin/pytest tests/test_smoke.py -q -k "unit or exposure or health_check"`
  -> `17 passed, 149 deselected`.
- Full smoke suite: `.venv/bin/pytest tests/test_smoke.py -q` ->
  `166 passed in 2.23s`.

### Real behavior check

- Real `portfolio-sync` ran in demo mode and resolved demo balance
  `$1047.2456`, canonical unit `$15.7087` from `unit_fraction=0.015`, positions
  `0`.
- Real exposure reconciliation in demo mode measured the resting
  `KXMLBGAME-26JUL271910ATLNYM-ATL` order (`58 @ $0.49`, `$28.42` notional) at
  `1.809189u` using the same `$15.7087` unit.
- The sizing path measured the same order at `1.809189u` from the same
  portfolio-sync bankroll and unit.
- Real `health-check` ran in demo mode and exited non-zero. Verdict:
  `ok=false`, `alert=true`, reason
  `legacy configured bankroll.unit_usd diverges from canonical bankroll-derived unit`;
  exchange reconciliation itself was reachable and reported
  `exchange_open_order_exposure_units=1.809189`.
- Real `scheduled-demo-cycle` ran in demo mode with `dry_run=true`: `200`
  candidates, `2` edge passes, `2` trade-eligible rows, `0` new demo orders,
  `exit_code=1`. The no-order reason was a Kalshi demo API
  `503 Service Unavailable` from `/portfolio/events/orders` during
  `demo-execute`. A fresh post-cycle portfolio sync still showed only the
  existing `58 @ $0.49` resting order at `1.809189u`.
- No live execution env vars were set to live values and no live orders were
  placed.

---

## Round 22 - 2026-07-27 - Closing snapshots and honest validation reporting

### What changed in code

- Added a persistent `closing_snapshots` table and `closing-snapshot` phase. The
  pass scans open unsettled exposure, checks whether each market is inside the
  configurable close window, and stores the current candidate-ranker consensus
  probability plus Kalshi midpoint/executable prices keyed by ticker.
- Changed settlement audit to prefer stored closing snapshots for CLV, with a
  last-resort fallback to a legitimate pre-close candidate-ranker artifact. Rows
  with no recoverable close snapshot remain CLV-unmeasured instead of being
  counted as CLV misses.
- Added fee-adjusted executable CLV while preserving midpoint CLV diagnostics:
  `clv_cents` is now the fee-adjusted executable metric, and
  `clv_midpoint_cents`, `closing_kalshi_mid_cents`, and
  `closing_executable_cents` are persisted for auditability.
- Added `validation-report` with per-market-family and per-signal-source
  performance, CLV measured counts, CLV beat rates, Brier-vs-baseline gaps,
  explicit distance to live thresholds, and concentration diagnostics.
- Added concentration visibility to status, scheduled demo-cycle reporting, and
  the local dashboard. A track record carried by one winner is flagged when that
  winner exceeds `NBABOT_CONCENTRATION_MAX_WINNER_SHARE` (default `0.50`).
- Kept live gates unchanged. This round made CLV measurable going forward; it did
  not lower validation thresholds, live env gates, qual/QAQ live hard blocks, or
  edge/plausibility gates.

### Tests

- Full suite: `.venv/bin/pytest -q` -> `152 passed in 2.09s`.
- Added tests for stored closing snapshots feeding settlement CLV on yes and no
  sides, fee-adjusted executable CLV differing from midpoint CLV, unmeasured CLV
  not validating a family, and concentration diagnostics firing only when one
  winner dominates.
- Added missing lightweight provider fixtures required by existing parser tests.
- Existing guardrail and live-gate tests remained enabled and passing.

### Real behavior check

- Real SQLite before backfill: `27` settlement records, `27` missing
  `closing_consensus_prob`, `27` missing `clv_cents`, and `0` stored
  `closing_snapshots`.
- Real `settlement-audit` backfill checked all `27` missing-CLV settlements and
  recovered `0`. All `27` remain honestly unmeasurable because no stored closing
  snapshots existed and no pre-close candidate-ranker artifact could recover the
  close line.
- Real measurable CLV beat rate remains unmeasured: `0` CLV-measured rows,
  `0` beats, `avg_clv=None`.
- Real validation report: `27` settled across `9` groups. Overall P&L is
  `$43.23`; largest winner is `KXMLBGAME-26JUL101915BOSNYM-BOS` at `$47.60`;
  P&L excluding that winner is `-$4.37`; largest-winner share is `110.1%`, so
  the concentration flag is on.
- Real scheduled demo cycle ran with `NBABOT_EXECUTION_MODE=demo` and
  `NBABOT_DELIVER_TO=stdout`: `200` candidates, `2` edge passes, `2`
  trade-eligible rows, `0` demo orders placed, `closing_snapshot checked=0
  recorded=0 skipped=0`, and `hard_error=none`. Demo execution was blocked by
  existing exposure gating (`game exposure 6.680 units <= max 5.000`).
- No live execution env vars were set to live values and no live orders were
  placed.

---

## Round 21 - 2026-07-09 - Demo late-fill reconciliation and bankroll sizing

### What changed in code

- Added demo order reconciliation with post-hoc status/fill refresh from Kalshi
  demo, idempotent fill recording, lifecycle status storage, and maker
  cancellation for stale resting orders using
  `DELETE /portfolio/events/orders/{id}`.
- Wired reconciliation into `settlement-audit` so late fills are captured before
  grading, while terminal unfilled cancellations are excluded from CLV/Brier
  settlement grading and included in fill-rate metrics.
- Added bankroll-based unit sizing: one unit is the clamped
  `NBABOT_UNIT_FRACTION` of active bankroll, paper falls back to
  `NBABOT_PAPER_BANKROLL`, demo reads the synced demo balance, and every intent
  persists unit size, units staked, stake dollars, and contracts.
- Added `NBABOT_MAX_ORDER_NOTIONAL_FRACTION` as a hard per-order notional
  backstop and kept existing 5-unit, daily, per-game, and live-gate controls.
- Added fill-rate visibility to status, dashboard, and scheduled demo reports.

### Tests

- Full suite: `.venv/bin/pytest` -> `149 passed in 2.01s`.
- Added mocked coverage for late-fill reconciliation, settlement grading from
  reconciled fills, idempotent reruns, unfilled cancellation exclusion, correct
  cancel path behavior, unit-fraction clamping, contract floor math, and notional
  backstop blocking.

### Real behavior check

- Real demo portfolio sync read `balance_dollars=999.9827`, positions `0`, from
  the Kalshi demo account. The active unit size was `$14.9997` at
  `NBABOT_UNIT_FRACTION=0.015`.
- Real demo reconciliation first canceled `2` stale resting maker orders. After
  adding support for the real `/portfolio/fills` schema, the corrected pass
  inserted `3` late fills from executed demo orders:
  - `KXMLBTOTAL-26JUL081840NYYTB-7` YES `1` @ `58c`, fee `0c`.
  - `KXMLBTOTAL-26JUL081840NYYTB-8` YES `1` @ `45c`, fee `0c`.
  - `KXMLBTOTAL-26JUL081910KCNYM-6` YES `1` @ `53c`, fee `0c`.
- Real settlement audit graded those `3` fills: `1` win, `2` losses, total P&L
  `-56c`, entry-model Brier `0.252867`, CLV unavailable because no pre-close
  consensus snapshot existed for those tickers.
- Balance checksum did not fully reconcile from these three fills alone:
  account balance is `999.9827` (`-1.73c` from `$1000`), while these newly
  graded fills net to `-56c`. The demo `/portfolio/settlements` endpoint also
  reports an older 44c/fee 1.73c settled fill whose order is not present in the
  current six recorded demo-order rows, so the account-level delta includes
  activity outside the current demo order ledger.
- One real scheduled demo cycle ran with `NBABOT_EXECUTION_MODE=demo`: `200`
  candidates, `0` edge passes, `0` trade-eligible, `0` demo orders placed,
  `blocked=no-candidates`, `hard_error=none`.
- No live execution env vars were set to live values and no live orders were
  placed.

---

## Round 25 - 2026-07-28 - Retrieval-grounded qualitative scenario engine

### What changed in code

- Replaced raw recency-only qual context with a local retrieval corpus built from the bot's own accumulated experience: `news_items`, `settlement_records`, prior `qual_signals` scenario branches, `qual_postmortems`, `qual_lessons`, and `closing_snapshots` when present.
- Added stdlib-only local embeddings (`hashbow-v1`, 96 dimensions) stored in SQLite. The corpus is small enough for brute-force cosine over a few thousand chunks, so no vector DB or ML stack was added.
- Added source-specific chunking: news stays one source item per chunk, settlement and closing rows become compact ledger summaries, scenario signals chunk per saved branch, and postmortems/lessons preserve the judgment/evidence trail.
- Added hybrid retrieval with dense cosine plus BM25-ish lexical scoring, reciprocal-rank fusion, and a deterministic reranker that rewards team, market-family, and precedent source matches.
- Added an explicit no-lookahead guard: retrieval excludes chunks with `source_timestamp` after a market close/cutoff, and tests deliberately prove after-close chunks are not retrievable.
- Added anti-leakage branch schema fields: `evidence_ids` and `status`. Unsupported branches are recorded for analysis but do not count toward tradeable probability.
- Added deterministic groundedness scoring and persistence. Signals below `NBABOT_QUAL_MIN_GROUNDEDNESS` default `0.6` are recorded as not tradeable with a blocker reason.
- Added `qual-index` phase plus status, UI, and validation-report telemetry for corpus size, retrieval count, mean groundedness, unsupported-branch rate, and low-groundedness blocks.

### Tests

- Full suite: `.venv/bin/pytest -q` -> `172 passed in 2.28s`.
- Compile pass: `.venv/bin/python -m compileall src/nbabot tests/test_smoke.py`.
- `git diff --check` passed.
- New tests cover incremental/idempotent indexing, provenance round trip, no-lookahead filtering, hybrid retrieval and reranking, unsupported branch mass, groundedness blocking, and LLM-unavailable fail-soft behavior.

### Real behavior check

- Real index build: `.venv/bin/nbabot qual-index` completed against `data/research.sqlite`.
- Real corpus after indexing: `4,295` chunks before the qual run, then `4,320` after new signals were recorded. Source counts after the run: `news_items=831`, `qual_signals=3446`, `settlement_records=28`, `qual_postmortems=6`, `qual_lessons=9`. `closing_snapshots` had no populated rows in this DB at verification time.
- Real no-lookahead guard: using the earliest settled ticker cutoff (`KXMLBGAME-26JUN221940LADMIN-MIN`, cutoff `2026-07-07T20:23:55.822279+00:00`), the corpus contained `4,214` after-cutoff chunks; retrieval returned `50` contexts and `0` after-cutoff contexts.
- Real qual-research pass: `.venv/bin/nbabot qual-research` exited cleanly with `status=ok`, `engine=codex`, `markets=23`, `news=60`, `produced=6`, `accepted=3`, `inserted=3`.
- Example real retrieval-grounded signal: `KXMLBGAME-26JUL281840BALDET-DET`, `qual_prob=0.53`, `tradeable=true`, `groundedness=0.825`. All four branches carried `status=supported` with evidence ids including `c0a128741b0aeed545ab1f8d`, `cfe25f9381d18cde6684c83e`, `25f21fdba2c2d1d436bfc189`, and `06a6788c5e0ca401354ba0a0`.
- Stored groundedness metrics for the real run: `3` scores, mean groundedness `0.825`, unsupported-branch rate `0.25`, low-groundedness blocked `0`.
- No live execution env vars were set to live values and no live orders were placed.

---

## Round 20 - 2026-07-07 - Phase 16 fee-adjusted edges, QAQ, fallback, and baskets

### What changed in code

- Added a pure Kalshi fee model in `fees.py` and threaded fee-adjusted `net_edge`
  through the edge engine, ranker, research candidates, trade intents, order
  records, audit records, and paper/demo fill metadata. Scheduled-cycle ranking
  now defaults to maker pricing; taker IOC remains available for triggered orders.
- Changed all edge-vs-floor checks to use net edge while preserving raw edge for
  diagnostics. `ExecutablePrice.as_dict()` now includes computed `price_cents` and
  `price_prob` so downstream orders use the same maker/taker price the ranker
  evaluated.
- Retired the flat confluence agreement bonus. Agreement diagnostics still persist,
  but `edge_bonus` is zero and agreement no longer lowers the demo/paper floor.
  The disagreement veto and shadow-trade path remain active.
- Added first-class `qual_activated_quant` plumbing through ranker rows, risk,
  intents, order ledgers, settlement learning families, and live hard blocks.
  QAQ can only remove the dynamic edge-floor blocker after a justified near-miss
  investigation; identity, freshness, spread, plausible-edge, composite, and
  disagreement-veto blockers remain hard blockers.
- Added near-miss selection with `NBABOT_NEAR_MISS_WINDOW`,
  `NBABOT_NEAR_MISS_INVESTIGATIONS_PER_CYCLE`, and `NBABOT_QAQ_FLOOR_BONUS`.
  Verdicts are strict JSON, citation-gated, persisted in SQLite, and fail soft.
- Added Claude fallback support through the official `anthropic` SDK dependency.
  The shared runner covers qual research, near-miss investigations, and
  postmortems. Empty `ANTHROPIC_API_KEY` keeps the previous codex-only fail-soft
  behavior.
- Added news-trigger market baskets. `news-watch` now builds and records targeted
  baskets for a triggered team's game markets, activates the basket during the
  scheduled demo cycle, and falls back to the full cycle if the basket is empty.

### Tests

- Full suite: `.venv/bin/pytest -q` -> `146 passed in 2.23s`.
- Compile pass: `.venv/bin/python -m compileall src/nbabot tests/test_smoke.py`.
- `git diff --check` passed.
- Deliberately rewritten expectations:
  - `test_demo_execution_builds_v2_payload` now expects default maker GTC payloads.
  - Edge/ranker floor tests now assert `raw_edge` plus fee-adjusted `net_edge`.
  - `test_candidate_ranker_confluence_agreement_does_not_lower_demo_required_base`
    replaces the old agreement-boost expectation because Phase 16 removed that
    flat bonus.
- Unweakened behavior verified: fuzzy matches still never pass, plausible-edge
  guard still blocks absurd edges, demo floor semantics still pass/block around
  the fee-adjusted threshold, and qual/QAQ are hard-blocked in live.

### Real behavior check

- Official Kalshi fee schedule was checked during implementation: current public
  factors are taker `0.07` and maker `0.0175`.
- Existing real fee evidence in this checkout is live-order data, not demo-order
  data with `average_fee_paid`. The requested 44c fill comparison used the real
  recorded 44c order in `data/SLATE-2026-JUN29-JUL01.live_orders.jsonl`: observed
  average fee `1.7200c` vs model taker prediction `1.7248c` per contract
  (`-0.0048c` difference). A 42c recorded fill was similarly close:
  observed `1.7000c` vs predicted `1.7052c`.
- Isolated paper/dry-run real cycle ran in `/tmp/nbabot-phase16-MLwAeX` with
  `NBABOT_EXECUTION_MODE=paper`, `NBABOT_DRY_RUN=1`, and no live ack vars. It
  produced `200` ranked candidates, `4` edge passes, `4` trade-eligible rows, and
  no order artifacts. Top ranked rows showed raw-vs-net fee adjustment, e.g.
  `KXMLBGAME-26JUL082210COLLAD-LAD` raw edge `0.14620`, net edge `0.14233`,
  fee probability `0.003869`, maker role.
- The isolated cycle selected `2` near misses and persisted `2` investigation
  rows. Both verdicts were not justified (`confidence=0.0`), so there were `0`
  QAQ upgrades in real data.
- Real qual research in that isolated cycle failed soft under the verification
  timeout cap: `status=unavailable`, `reason=timeout`, `engine=codex`, `accepted=0`.
- `ANTHROPIC_API_KEY` is empty by choice. A real no-key invocation returned
  `status=unavailable`, `reason=command not found`, `engine=codex`, `signals=0`;
  Claude was not invoked and no key material was printed.
- No live execution env vars were set to live values and no live orders were
  placed.
