"""Smoke tests — no network. Everything is constructed from fixtures/mocks.

These also lock in the honesty contract (AGENTS.md §0): if you weaken the guardrails,
these fail. Do not delete the assertions to make a build pass.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("KALSHI_API_KEY", "test")
os.environ.setdefault("NBABOT_GAME_ID", "NBA-2026-FINALS-G3")

from nbabot import (  # noqa: E402
    backtesting,
    calibration,
    execution,
    guardrails,
    market_discovery,
    research,
    risk,
    scenarios,
    sizing,
    soccer_research,
    ui,
)
from nbabot.agents import PHASES, reconcile  # noqa: E402
from nbabot.kalshi import Quote, _TITLE_RE  # noqa: E402
from nbabot.scores import GameState, PlayerLine  # noqa: E402


# ── guardrails (the contract) ───────────────────────────────────────────────────
def test_guardrail_footer_present_and_helpline():
    assert "HOPENY" in guardrails.GUARDRAIL_FOOTER
    assert "877-8-HOPENY" in guardrails.GUARDRAIL_FOOTER
    assert len(guardrails.STANDING_ORDERS) >= 5
    assert guardrails.MAX_STAKE_UNITS == 5.0
    assert any("<= 5 units" in order for order in guardrails.STANDING_ORDERS)


def test_with_footer_idempotent():
    once = guardrails.with_footer("bet idea")
    twice = guardrails.with_footer(once)
    assert once == twice
    assert once.count(guardrails.GUARDRAIL_FOOTER) == 1


def test_hope_bet_flag():
    assert guardrails.is_hope_bet(5)
    assert not guardrails.is_hope_bet(3)


# ── kalshi title parsing ────────────────────────────────────────────────────────
@pytest.mark.parametrize("title,name,line,stat", [
    ("Karl-Anthony Towns: 15+ points", "Karl-Anthony Towns", "15", "points"),
    ("Stephon Castle: 4+ rebounds", "Stephon Castle", "4", "rebounds"),
    ("Jalen Brunson: 6+ assists", "Jalen Brunson", "6", "assists"),
])
def test_title_regex(title, name, line, stat):
    m = _TITLE_RE.match(title)
    assert m and m.group("name") == name and m.group("line") == line
    assert stat in m.group("stat").lower()


def test_quote_implied():
    q = Quote(bid=38, ask=39, ticker="X")
    assert q.mid == 39 or q.mid == 38 or q.mid == round((38 + 39) / 2)
    assert 0 <= q.implied <= 1


def test_discovery_catalog_maps_configured_lines():
    scen, _, _ = scenarios.load_scenarios(_doc())
    raw = [
        {
            "series": "KXNBAPTS",
            "ticker": "KXNBAPTS-26JUN08SASNYK-BRUNSON-27",
            "title": "Jalen Brunson: 27+ points",
            "yes_bid_dollars": "0.44",
            "yes_ask_dollars": "0.46",
        },
        {
            "series": "KXNBAGAME",
            "ticker": "KXNBAGAME-26JUN08SASNYK-NYK",
            "title": "New York Knicks win",
            "yes_bid_dollars": "0.54",
            "yes_ask_dollars": "0.56",
        },
    ]

    rows = market_discovery.build_catalog("GAME", "26JUN08SASNYK", raw, scen)

    brunson = next(r for r in rows if r["ticker"].endswith("BRUNSON-27"))
    knicks = next(r for r in rows if r["ticker"].endswith("NYK"))
    assert brunson["mapping_status"] == "mapped"
    assert "brunson_points" in brunson["mapped_markets"]
    assert "S3" in brunson["mapped_scenarios"]
    assert knicks["mapping_status"] == "mapped"
    assert "knicks_win" in knicks["mapped_markets"]


# ── scenario loading + evaluation (pure) ────────────────────────────────────────
def _doc():
    import yaml
    p = Path(__file__).resolve().parents[1] / "config" / "NBA-2026-FINALS-G3.scenarios.yaml"
    return yaml.safe_load(p.read_text())


def test_load_scenarios():
    scen, mm, hc = scenarios.load_scenarios(_doc())
    ids = {s.id for s in scen}
    assert {"S1", "S4", "S6", "S7"} <= ids
    s4 = next(s for s in scen if s.id == "S4")
    assert any(leg.player == "towns" and leg.stat == "rebounds" for leg in s4.legs)


def test_evaluate_marks_under_dead_when_busted():
    scen, _, _ = scenarios.load_scenarios(_doc())
    s2 = next(s for s in scen if s.id == "S2")  # has wemby_points < 24
    gs = GameState(state="in", period=3, clock="5:00",
                   home_abbr="NYK", away_abbr="SAS",
                   players={"victor wembanyama": PlayerLine("Victor Wembanyama", pts=30)})
    ss = scenarios.evaluate(s2, props={}, winners={}, gs=gs)
    assert ss.state == "DEAD"  # wemby already at 30, "<24" busted


def test_evaluate_on_track_pregame():
    scen, _, _ = scenarios.load_scenarios(_doc())
    s4 = next(s for s in scen if s.id == "S4")
    gs = GameState(state="in", period=1, clock="12:00")
    ss = scenarios.evaluate(s4, props={}, winners={}, gs=gs)
    assert ss.state in ("ON_TRACK", "DRIFTING")  # nothing busted at tip


def test_resolve_win_uses_score_with_espn_aliases():
    gs = GameState(state="post", home_abbr="NY", away_abbr="SA",
                   home_score=111, away_score=115, home_wp=0.0)
    spurs = scenarios.Leg("spurs_win", "==", 1, 0.5, special="win", team="SAS")
    knicks = scenarios.Leg("knicks_win", "==", 1, 0.5, special="win", team="NYK")

    assert scenarios.resolve_leg(spurs, gs) == 1
    assert scenarios.resolve_leg(knicks, gs) == 0


def test_unresolvable_special_leg_stays_unresolved():
    gs = GameState(state="post", home_score=120, away_score=110)
    total = scenarios.Leg("game_total", ">", 216.5, 0.5,
                          special="total", resolvable=False)

    assert scenarios.resolve_leg(total, gs) is None


# ── calibration ─────────────────────────────────────────────────────────────────
def test_brier_baseline():
    rows = [{"prior_p": 0.5, "outcome": 1}, {"prior_p": 0.5, "outcome": 0}]
    assert abs(calibration.brier(rows) - 0.25) < 1e-9


def test_calibration_overrides_are_conservative():
    scen, _, hc = scenarios.load_scenarios(_doc())
    overrides = {
        "sgp_haircut": {"S1": 9.0, "S2": 0.4},
        "family_prior_multipliers": {"points": 0.9},
        "market_prior_multipliers": {"towns_rebounds": 1.2},
    }

    adjusted, adjusted_hc = calibration.apply_overrides(scen, hc, overrides)
    s3 = next(s for s in adjusted if s.id == "S3")
    brunson_points = next(l for l in s3.legs if l.market == "brunson_points")
    s4 = next(s for s in adjusted if s.id == "S4")
    towns_rebounds = next(l for l in s4.legs if l.market == "towns_rebounds")

    assert adjusted_hc["S1"] == 1.0
    assert adjusted_hc["S2"] == 0.4
    assert brunson_points.prior_p == 0.405
    assert towns_rebounds.prior_p == 0.58


def test_recompute_repairs_legacy_s7_unresolved_haircut(tmp_path):
    root = Path(__file__).resolve().parents[1]
    saved = json.loads((root / "data" / "NBA-2026-FINALS-G3.reconcile.json").read_text())
    assert saved["summary"]["haircut"]["S7"] == 18.403

    log_path = tmp_path / "NBA-2026-FINALS-G3.log.jsonl"
    log_path.write_text((root / "data" / "NBA-2026-FINALS-G3.log.jsonl").read_text())

    summary = calibration.recompute(log_path)

    assert summary["haircut"]["S7"] == 9.201
    assert "total" not in summary["families"]


def test_reconcile_excludes_unresolved_leg_from_scenario_prior(tmp_path, monkeypatch):
    scen, _, _ = scenarios.load_scenarios(_doc())
    s7 = next(s for s in scen if s.id == "S7")
    gs = GameState(
        state="post",
        home_abbr="NY",
        away_abbr="SA",
        home_score=110,
        away_score=120,
        players={
            "stephon castle": PlayerLine(
                "Stephon Castle", pts=16, ast=5, threes=2),
        },
    )

    class DummySettings:
        game_id = "TEST-GAME"
        deliver_to = "stdout"

        def data_path(self, suffix):
            return tmp_path / f"{self.game_id}.{suffix}"

    class DummyKalshi:
        def prop_prices(self, game_tag):
            return {}

        def winner_prices(self, game_tag):
            return {}

    class DummyContext:
        settings = DummySettings()
        kalshi = DummyKalshi()
        scenarios = [s7]
        game_tag = "TEST-GAME"

        def read_json(self, suffix):
            return None

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(reconcile, "resolve_event_id", lambda ctx: "event-id")
    monkeypatch.setattr(reconcile.scores, "get_game_state", lambda event_id: gs)
    monkeypatch.setattr(reconcile, "deliver", lambda *args, **kwargs: None)

    result = reconcile.run(DummyContext())
    rows = calibration.load_log(tmp_path / "TEST-GAME.log.jsonl")
    s7_row = rows[0]["scenarios"][0]

    assert s7_row["prior_p_joint"] == 0.10868
    assert s7_row["resolved_legs"] == 3
    assert s7_row["total_legs"] == 4
    assert rows[0]["legs"][-1]["market"] == "spurs_cover"
    assert rows[0]["legs"][-1]["outcome"] is None
    assert result["summary"]["haircut"]["S7"] == 9.201


# ── research / execution framework ──────────────────────────────────────────────
class _ExecSettings:
    game_id = "TEST-GAME"
    execution_mode = "paper"
    live_trading_ack = ""
    dry_run = True
    demo_api_base = "https://demo-api.kalshi.co/trade-api/v2"
    max_daily_loss_units = 2.0
    max_game_exposure_units = 5.0
    min_edge = 0.05
    stale_market_seconds = 90
    max_spread_cents = 10
    unit_usd = 1.0
    deliver_to = "stdout"

    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.research_db_path = tmp_path / "research.sqlite"
        self.kill_switch_path = tmp_path / "KILL_SWITCH"

    def data_path(self, suffix):
        return self.data_dir / f"{self.game_id}.{suffix}"


def _intent(**overrides):
    base = {
        "game_id": "TEST-GAME",
        "scenario_id": "S1",
        "ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "contracts": 1,
        "price_cents": 50,
        "stake_units": 0.5,
        "model_prob": 0.60,
        "market_prob": 0.50,
        "edge": 0.10,
        "sgp_adjusted_prob": 0.20,
        "risk": 3,
        "hope_bet": False,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "bid_cents": 48,
        "ask_cents": 50,
        "rationale": "test",
    }
    base.update(overrides)
    return execution.TradeIntent(**base)


def test_risk_gate_rejects_core_failures(tmp_path):
    settings = _ExecSettings(tmp_path)
    assert risk.evaluate_trade_intent(_intent(stake_units=5.0), settings).approved
    assert not risk.evaluate_trade_intent(_intent(stake_units=5.01), settings).approved
    assert "missing SGP-adjusted probability" in risk.evaluate_trade_intent(
        _intent(sgp_adjusted_prob=None), settings).reasons
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert not risk.evaluate_trade_intent(_intent(captured_at=stale), settings).approved
    assert not risk.evaluate_trade_intent(_intent(risk=5, hope_bet=False), settings).approved
    settings.kill_switch_path.write_text("stop")
    assert not risk.evaluate_trade_intent(_intent(), settings).approved


def test_capped_kelly_uses_five_unit_default_cap():
    result = sizing.capped_kelly(
        edge=0.8,
        market_prob=0.5,
        entry_price_cents=50,
        unit_cents=100,
        multiplier=10.0,
    )

    assert result.stake_units == guardrails.MAX_STAKE_UNITS
    assert result.contracts == 10


def test_soccer_expected_goals_fit_and_scoreline_probability():
    panama_xg = soccer_research.fit_expected_goals({1: 0.565, 2: 0.195, 3: 0.055})
    croatia_xg = soccer_research.fit_expected_goals(
        {1: 0.875, 2: 0.635, 3: 0.365, 4: 0.175}
    )
    professional = soccer_research.scoreline_probability(
        croatia_xg,
        panama_xg,
        lambda croatia, panama: panama == 0 and croatia in (2, 3),
    )

    assert panama_xg == pytest.approx(0.825, abs=0.002)
    assert croatia_xg == pytest.approx(2.153, abs=0.002)
    assert professional == pytest.approx(0.203, abs=0.002)


def test_soccer_uncertainty_adjust_is_conservative():
    assert soccer_research.uncertainty_adjust(0.40) == pytest.approx(0.36)


def test_paper_execution_is_idempotent(tmp_path):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent()
    decision = risk.evaluate_trade_intent(intent, settings)

    first = execution.execute_paper(intent, decision, settings, store, audit)
    second = execution.execute_paper(intent, decision, settings, store, audit)

    assert first.status == "filled"
    assert second.client_order_id == first.client_order_id
    assert store.count_orders("paper_orders") == 1


def test_demo_execution_builds_v2_payload(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent()
    decision = risk.evaluate_trade_intent(intent, settings)

    class DummyKalshi:
        body = None

        def demo_place_order(self, demo_api_base, body):
            self.body = body
            return {"order": {"client_order_id": body["client_order_id"]}}

    kalshi = DummyKalshi()
    receipt = execution.execute_demo(intent, decision, settings, store, audit, kalshi)

    assert receipt.status == "submitted"
    assert kalshi.body["ticker"] == "KXTEST"
    assert kalshi.body["side"] == "bid"
    assert kalshi.body["price"] == "0.5000"
    assert kalshi.body["count"] == "1.00"
    assert kalshi.body["time_in_force"] == "immediate_or_cancel"
    assert "action" not in kalshi.body
    assert "type" not in kalshi.body
    assert store.count_orders("demo_orders") == 1


def test_live_execution_requires_explicit_gates(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent()
    decision = risk.evaluate_trade_intent(intent, settings)

    class DummyKalshi:
        def place_order(self, body):
            return {"order_id": "should-not-run"}

    with pytest.raises(RuntimeError, match="NBABOT_DRY_RUN=0"):
        execution.execute_live(intent, decision, settings, store, audit, DummyKalshi())


def test_live_execution_submits_v2_order_when_gated(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"
    settings.dry_run = False
    settings.live_trading_ack = "LIVE_TRADES_REAL_MONEY"
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent()
    decision = risk.evaluate_trade_intent(intent, settings)

    class DummyKalshi:
        body = None

        def place_order(self, body):
            self.body = body
            return {"order_id": "live-1", "client_order_id": body["client_order_id"]}

    kalshi = DummyKalshi()
    receipt = execution.execute_live(intent, decision, settings, store, audit, kalshi)

    assert receipt.status == "submitted"
    assert kalshi.body["ticker"] == "KXTEST"
    assert kalshi.body["side"] == "bid"
    assert store.count_orders("live_orders") == 1
    assert store.game_order_exposure_units("live_orders", "TEST-GAME") == 0.5


def test_backtest_rebuilds_s7_without_unresolved_leg(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scen, _, _ = scenarios.load_scenarios(_doc())
    log_path = tmp_path / "NBA-2026-FINALS-G3.log.jsonl"
    log_path.write_text((root / "data" / "NBA-2026-FINALS-G3.log.jsonl").read_text())

    metrics = backtesting.run_backtest("NBA-2026-FINALS-G3", log_path, scen)
    s7 = next(r for r in metrics["scenario_rows"] if r["scenario_id"] == "S7")

    assert s7["prior_p_joint"] == 0.10868
    assert metrics["scenario_count"] == 7


def test_ui_renders_dashboard_without_server(tmp_path):
    scen, _, _ = scenarios.load_scenarios(_doc())

    class DummySettings(_ExecSettings):
        kalshi_game_tag = "TEST"

    class DummyContext:
        settings = DummySettings(tmp_path)
        scenarios = scen

        def read_json(self, suffix):
            return None

    html = ui.render_dashboard(DummyContext())

    assert "NBA Scenario Bot" in html
    assert "Guarded execution" in html


def test_new_automation_phases_registered():
    assert "discover-markets" in PHASES
    assert "autopilot" in PHASES
    assert "live-execute" in PHASES
