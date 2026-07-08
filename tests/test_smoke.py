"""Smoke tests — no network. Everything is constructed from fixtures/mocks.

These also lock in the honesty contract (AGENTS.md §0): if you weaken the guardrails,
these fail. Do not delete the assertions to make a build pass.
"""
import base64
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

os.environ.setdefault("KALSHI_API_KEY", "test")
os.environ.setdefault("NBABOT_GAME_ID", "NBA-2026-FINALS-G3")

from nbabot import (  # noqa: E402
    alerts,
    backtest_replay,
    backtesting,
    calibration,
    candidate_ranker as candidate_ranker_core,
    confluence,
    cli,
    edge_engine,
    execution,
    fees,
    guardrails,
    market_identity,
    market_matcher as market_matcher_core,
    market_discovery,
    orderbook,
    odds_math,
    odds_refresh,
    performance_learner,
    qual_learning,
    qual_postmortem as qual_postmortem_core,
    qual_research as qual_research_core,
    research_news,
    news_watch as news_watch_core,
    research,
    risk,
    scenarios,
    settlement_audit as settlement_audit_core,
    sizing,
    slate,
    soccer_research,
    sports,
    ui,
)
from nbabot.adapters import get_adapter  # noqa: E402
from nbabot.agents import (  # noqa: E402
    PHASES,
    book_watch,
    candidate_ranker,
    daily_cycle,
    demo_execute,
    discover_markets,
    historical_backtest,
    market_matcher,
    live_execute,
    news_ingest,
    news_watch,
    paper,
    portfolio_sync,
    qual_postmortem,
    ports,
    qual_research as qual_research_agent,
    reconcile,
    research_agent,
    scheduled_demo_cycle,
    settlement_audit,
    slate_discovery,
    slate_verify,
    snapshot_market,
    source_check,
    status,
    telegram_test,
)
from nbabot.config import load_settings  # noqa: E402
from nbabot.kalshi import DEMO_CREDENTIALS_BLOCKED_REASON, KalshiClient, Quote, _TITLE_RE  # noqa: E402
from nbabot.scores import GameState, PlayerLine  # noqa: E402
from nbabot.sources import registry as source_registry  # noqa: E402


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


def test_telegram_delivery_uses_bot_api_without_network(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append((url, json.loads(data), headers, timeout))

        class Response:
            pass

        return Response()

    monkeypatch.setenv("NBABOT_TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(alerts.requests, "post", fake_post)

    alerts.deliver("hello", "telegram:123")

    assert calls[0][0] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert calls[0][1]["chat_id"] == "123"
    assert calls[0][1]["text"] == "hello"


def test_telegram_test_phase_requires_telegram_target(monkeypatch):
    class DummySettings:
        game_id = "TEST-GAME"
        deliver_to = "stdout"

    class DummyContext:
        settings = DummySettings()

    messages = []
    monkeypatch.setattr(telegram_test, "deliver", lambda text, to="stdout": messages.append((text, to)))

    result = telegram_test.run(DummyContext())

    assert result["ok"] is False
    assert result["reason"] == "deliver-target-not-telegram"
    assert "NBABOT_DELIVER_TO" in messages[0][0]


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


def test_settings_selects_nba_adapter_from_config():
    settings = load_settings("NBA-2026-FINALS-G3")

    assert settings.sport == "nba"
    assert get_adapter(settings.sport).key == "nba"


def test_orderbook_derives_side_aware_prices_and_depth():
    book = orderbook.OrderBook.from_snapshot("KXTEST", {
        "yes": [[68, 100], [65, 300], [60, 200]],
        "no": [[28, 50], [27, 150], [25, 200]],
    })

    assert book.best_bid("yes") == 68
    assert book.best_ask("yes") == 72
    assert book.spread("yes") == 4
    assert book.depth_at_limit("yes", 73) == 200
    assert book.vwap_to_buy("yes", 100, 73) == (72.5, 100)

    book.apply_delta({"side": "yes", "price": 68, "delta": -100})
    assert book.best_bid("yes") == 65


def test_kalshi_orderbook_uses_rest_endpoint_without_network():
    class DummyKalshi(KalshiClient):
        path = None
        params = None

        def __init__(self):
            pass

        def _get(self, path, params=None):
            self.path = path
            self.params = params
            return {
                "orderbook": {
                    "yes": [[68, 100]],
                    "no": [[28, 50]],
                }
            }

    client = DummyKalshi()
    book = client.orderbook("KXTEST", depth=10)

    assert client.path == "/trade-api/v2/markets/KXTEST/orderbook"
    assert client.params == {"depth": 10}
    assert book.best_ask("yes") == 72


def test_kalshi_open_market_scan_does_not_require_series():
    class DummyKalshi(KalshiClient):
        params = None

        def __init__(self):
            pass

        def _get(self, path, params=None):
            self.params = params
            return {"markets": [{"ticker": "KXOPEN"}]}

    client = DummyKalshi()
    rows = client.list_open_markets(max_pages=1)

    assert rows == [{"ticker": "KXOPEN"}]
    assert client.params == {"limit": 500, "status": "open"}


def test_kalshi_market_lookup_uses_rest_endpoint_without_network():
    class DummyKalshi(KalshiClient):
        path = None

        def __init__(self):
            pass

        def _get(self, path, params=None):
            self.path = path
            return {"market": {"ticker": "KXTEST", "status": "settled"}}

    client = DummyKalshi()
    market = client.market("KXTEST")

    assert client.path == "/trade-api/v2/markets/KXTEST"
    assert market["ticker"] == "KXTEST"


def test_kalshi_demo_request_uses_demo_credentials_and_base(tmp_path):
    live_path = tmp_path / "live-key.pem"
    demo_path = tmp_path / "demo-key.pem"
    live_key = _write_test_private_key(live_path)
    demo_key = _write_test_private_key(demo_path)
    calls = []

    class CaptureSession:
        def request(self, method, url, headers=None, params=None, data=None, timeout=None):
            calls.append({
                "method": method,
                "url": url,
                "headers": headers or {},
                "params": params,
                "data": data,
                "timeout": timeout,
            })

            class Response:
                status_code = 200
                content = b"{}"

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"ok": True}

            return Response()

    client = KalshiClient(
        "live-key-id",
        live_path,
        "https://api.elections.kalshi.com",
        demo_api_key="demo-key-id",
        demo_private_key_path=demo_path,
    )
    client._s = CaptureSession()

    response = client.demo_place_order(
        "https://external-api.demo.kalshi.co/trade-api/v2",
        {"ticker": "KXTEST"},
    )

    assert response == {"ok": True}
    assert calls[0]["url"] == (
        "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/events/orders"
    )
    assert calls[0]["headers"]["KALSHI-ACCESS-KEY"] == "demo-key-id"
    message = (
        calls[0]["headers"]["KALSHI-ACCESS-TIMESTAMP"]
        + "POST"
        + "/trade-api/v2/portfolio/events/orders"
    ).encode()
    signature = base64.b64decode(calls[0]["headers"]["KALSHI-ACCESS-SIGNATURE"])
    demo_key.public_key().verify(
        signature,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    with pytest.raises(InvalidSignature):
        live_key.public_key().verify(
            signature,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )


def test_historical_backtest_phase_registered():
    assert PHASES["historical-backtest"] == historical_backtest.run


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

    class DummyAdapter:
        label = "NBA"

        def get_live_state(self, event_id):
            return gs

        def empty_game_state(self):
            return GameState()

        def market_quotes(self, kalshi, game_tag):
            return None

        def price_leg(self, leg, quotes):
            return None

        def resolve_leg(self, leg, game_state):
            return scenarios.resolve_leg(leg, game_state)

    class DummyContext:
        settings = DummySettings()
        kalshi = object()
        adapter = DummyAdapter()
        scenarios = [s7]
        game_tag = "TEST-GAME"

        def read_json(self, suffix):
            return None

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(reconcile, "resolve_event_id", lambda ctx: "event-id")
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
    sport = "nba"
    execution_mode = "paper"
    live_trading_ack = ""
    broad_slate_execution = ""
    research_override_ack = ""
    research_override_max_units = 1.0
    dry_run = True
    demo_api_base = "https://demo-api.kalshi.co/trade-api/v2"
    kalshi_demo_api_key = ""
    paper_demo_daily_trade_cap = 50
    qual_daily_trade_cap = 10
    max_daily_loss_units = 2.0
    max_daily_exposure_units = 5.0
    max_game_exposure_units = 5.0
    min_edge = 0.05
    demo_min_edge = 0.03
    qual_min_edge = 0.06
    confluence_agree_delta = 0.05
    confluence_edge_bonus = 0.01
    confluence_veto_delta = 0.08
    qual_signal_max_age_hours = 12
    qual_lessons_top_n = 5
    qual_llm_cmd = "/missing/codex exec"
    qual_llm_timeout_seconds = 600
    news_window_hours = 48
    news_user_agent = "nbabot-test/0.1"
    event_trigger_cooldown_minutes = 45
    event_trigger_daily_cap = 8
    max_plausible_edge = 0.15
    stale_market_seconds = 90
    max_spread_cents = 10
    unit_usd = 1.0
    deliver_to = "stdout"

    def __init__(self, tmp_path):
        self.data_dir = tmp_path
        self.research_db_path = tmp_path / "research.sqlite"
        self.kill_switch_path = tmp_path / "KILL_SWITCH"
        self.kalshi_demo_private_key_path = tmp_path / "kalshi-demo-private-key.txt"
        self.research_teams_path = tmp_path / "research_teams.yaml"

    def data_path(self, suffix):
        return self.data_dir / f"{self.game_id}.{suffix}"

    @property
    def execution_min_edge(self):
        if self.execution_mode in {"paper", "demo"}:
            return self.demo_min_edge
        return self.min_edge


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


def _write_test_private_key(path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key


def test_fee_model_curve_and_total_fee_round_up():
    assert fees.expected_fee(50, maker=False) == pytest.approx(1.75)
    assert fees.expected_fee(50, maker=True) == pytest.approx(0.4375)
    assert fees.expected_fee(1, maker=False) == pytest.approx(fees.expected_fee(99, maker=False))
    assert fees.expected_fee(1, maker=False) > fees.expected_fee(1, maker=True)
    assert fees.expected_fee_prob(50, maker=False) == pytest.approx(0.0175)
    assert fees.expected_total_fee_cents(50, 1, maker=False) == pytest.approx(2.0)


def test_order_request_uses_taker_ioc_when_requested():
    request = execution.OrderRequest(
        client_order_id="cid",
        ticker="KXTEST",
        action="buy",
        side="yes",
        order_type="limit",
        count=1,
        price_cents=50,
        liquidity_role="taker",
    )

    body = request.kalshi_v2_body()

    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["self_trade_prevention_type"] == "taker_at_cross"


def _settlement_fixture(
    idx,
    *,
    ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
    outcome=1,
    model_prob=0.70,
    market_prob=0.50,
    beat_closing=True,
    clv_cents=5.0,
):
    now = (datetime(2026, 7, 6, tzinfo=timezone.utc) + timedelta(seconds=idx)).isoformat()
    return {
        "client_order_id": f"settled-{idx}",
        "game_id": "TEST-GAME",
        "mode": "paper",
        "ticker": ticker,
        "side": "yes",
        "contracts": 1.0,
        "entry_price_cents": market_prob * 100,
        "audited_at": now,
        "settled_at": now,
        "market_status": "settled",
        "winning_side": "yes" if outcome else "no",
        "outcome": outcome,
        "payout_cents": 100.0 if outcome else 0.0,
        "pnl_cents": (100.0 if outcome else 0.0) - market_prob * 100,
        "entry_model_prob": model_prob,
        "entry_market_prob": market_prob,
        "closing_consensus_prob": market_prob + clv_cents / 100.0,
        "closing_consensus_cents": market_prob * 100 + clv_cents,
        "clv_cents": clv_cents,
        "beat_closing_consensus": beat_closing,
        "brier_entry_model": (model_prob - outcome) ** 2,
        "market": {
            "ticker": ticker,
            "series_ticker": ticker.split("-", 1)[0],
            "title": "Constructed settlement fixture",
            "market_type": "binary",
        },
        "closing_consensus": {
            "available": True,
            "prob": market_prob + clv_cents / 100.0,
            "price_cents": market_prob * 100 + clv_cents,
        },
    }


def _record_constructed_validated_mlb_family(store):
    for i in range(120):
        store.record_settlement(
            _settlement_fixture(
                i,
                ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
                outcome=1 if i < 80 else 0,
                model_prob=0.70,
                market_prob=0.50,
                beat_closing=i < 75,
                clv_cents=6.0 if i < 75 else -2.0,
            )
        )


RSS_FIXTURE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Yankees starter returns to lineup</title>
    <link>https://example.com/yankees-lineup</link>
    <description>New York Yankees get a key bat back.</description>
    <pubDate>Tue, 07 Jul 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Old Yankees note</title>
    <link>https://example.com/old-yankees</link>
    <description>Old item.</description>
    <pubDate>Fri, 03 Jul 2026 12:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


ATOM_FIXTURE = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Yankees bullpen discussion</title>
    <link href="https://reddit.example/yankees-bullpen" />
    <summary>Fans discuss Yankees bullpen fatigue.</summary>
    <updated>2026-07-07T13:00:00Z</updated>
  </entry>
</feed>"""


RECAP_RSS_FIXTURE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Yankees beat Red Sox after late rally</title>
    <link>https://example.com/yankees-recap</link>
    <description>New York Yankees beat Boston 5-4 after the bullpen held late.</description>
    <pubDate>Tue, 07 Jul 2026 05:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Yankees starter returns to lineup</title>
    <link>https://example.com/yankees-lineup-not-recap</link>
    <description>New York Yankees get a key bat back.</description>
    <pubDate>Tue, 07 Jul 2026 12:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_news_ingest_parses_dedups_and_fails_soft(tmp_path):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    teams = [
        research_news.ResearchTeam(
            key="yankees",
            canonical_name="New York Yankees",
            aliases=("New York Yankees", "Yankees"),
            rss_feeds=(
                {"name": "mlb_yankees", "url": "https://example.com/rss"},
                {"name": "espn_mlb", "url": "https://example.com/blocked", "require_alias": True},
            ),
            subreddits=("NYYankees",),
        )
    ]

    def fetcher(url, user_agent):
        assert user_agent == "nbabot-test/0.1"
        if url.endswith("/blocked"):
            import urllib.error
            raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)
        if url.endswith("/new.rss"):
            return 200, ATOM_FIXTURE
        return 200, RSS_FIXTURE

    payload = research_news.ingest_team_feeds(
        teams=teams,
        store=store,
        user_agent=settings.news_user_agent,
        window_hours=48,
        request_delay_seconds=0,
        fetcher=fetcher,
        now=datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc),
    )
    second = research_news.ingest_team_feeds(
        teams=teams,
        store=store,
        user_agent=settings.news_user_agent,
        window_hours=48,
        request_delay_seconds=0,
        fetcher=fetcher,
        now=datetime(2026, 7, 7, 14, 0, tzinfo=timezone.utc),
    )

    assert payload["team_counts"]["yankees"]["parsed"] == 2
    assert payload["team_counts"]["yankees"]["inserted"] == 2
    assert payload["team_counts"]["yankees"]["ignored_old"] == 1
    assert second["team_counts"]["yankees"]["inserted"] == 0
    assert any(row["status"] == "http_429" for row in payload["source_status"])
    recent = store.recent_news_items(["yankees"], window_hours=48)
    assert {row["url"] for row in recent} == {
        "https://example.com/yankees-lineup",
        "https://reddit.example/yankees-bullpen",
    }


def test_recap_matching_uses_team_and_game_date():
    teams = [
        research_news.ResearchTeam(
            key="yankees",
            canonical_name="New York Yankees",
            aliases=("New York Yankees", "Yankees", "NYY"),
            rss_feeds=({"name": "mlb_yankees", "url": "https://example.com/rss"},),
            subreddits=(),
        )
    ]
    record = _settlement_fixture(
        1,
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        outcome=1,
        model_prob=0.60,
    )

    def fetcher(url, user_agent):
        return 200, RECAP_RSS_FIXTURE

    recap = qual_postmortem_core.fetch_recap_for_settlement(
        record=record,
        signal={"analysis": {"market": {"teams": ["yankees"]}}},
        teams=teams,
        user_agent="nbabot-test/0.1",
        fetcher=fetcher,
    )

    assert recap["recap_status"] == "found"
    assert recap["team"] == "yankees"
    assert recap["item"]["url"] == "https://example.com/yankees-recap"
    assert recap["game_date"] == "2026-07-06"


def test_recap_matching_missing_path_is_honest():
    teams = [
        research_news.ResearchTeam(
            key="yankees",
            canonical_name="New York Yankees",
            aliases=("New York Yankees", "Yankees", "NYY"),
            rss_feeds=({"name": "mlb_yankees", "url": "https://example.com/rss"},),
            subreddits=(),
        )
    ]
    record = _settlement_fixture(1, ticker="KXMLBGAME-26JUL06NYYBOS-NYY")

    def fetcher(url, user_agent):
        return 200, RSS_FIXTURE

    recap = qual_postmortem_core.fetch_recap_for_settlement(
        record=record,
        signal={"analysis": {"market": {"teams": ["yankees"]}}},
        teams=teams,
        user_agent="nbabot-test/0.1",
        fetcher=fetcher,
    )

    assert recap["recap_status"] == "missing"
    assert recap["reason"] == "no-matching-recap"
    assert recap["source_status"][0]["status"] == "ok"


def test_news_watch_keyword_matching_uses_word_boundaries():
    assert news_watch_core.keyword_hits("Starter is out for the season", ["out for"])
    assert news_watch_core.keyword_hits("Slugger placed on IL", ["IL"])
    assert news_watch_core.keyword_hits("Lineup change announced", ["lineup change"])
    assert not news_watch_core.keyword_hits("Outfield alignment changes", ["out"])


def test_news_watch_debounce_and_daily_cap_logic():
    now = datetime(2026, 7, 7, 18, 0, tzinfo=timezone.utc)
    recent_history = [
        {
            "team": "yankees",
            "decision": "fired",
            "fired_at": (now - timedelta(minutes=10)).isoformat(),
        }
    ]
    debounced = news_watch_core.trigger_decision(
        "yankees",
        history=recent_history,
        now=now,
        cooldown_minutes=45,
        daily_cap=8,
    )
    assert debounced["decision"] == "debounced"

    capped_history = [
        {
            "team": f"team-{idx}",
            "decision": "fired",
            "fired_at": (now - timedelta(minutes=idx)).isoformat(),
        }
        for idx in range(8)
    ]
    capped = news_watch_core.trigger_decision(
        "mets",
        history=capped_history,
        now=now,
        cooldown_minutes=45,
        daily_cap=8,
    )
    assert capped["decision"] == "capped"


def test_news_watch_no_hit_run_is_silent_and_does_not_cycle(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.research_teams_path.write_text("""
news_watch:
  high_impact_patterns:
    - scratched
teams:
  yankees:
    canonical_name: New York Yankees
    aliases: [Yankees]
    rss_feeds:
      - name: mlb_yankees
        url: https://example.com/yankees/rss
""")
    artifacts = {}

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

        def write_json(self, suffix, payload):
            artifacts[suffix] = payload
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    rss = b"""<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Yankees announce charity event</title>
    <link>https://example.com/yankees-charity</link>
    <description>No roster impact.</description>
    <pubDate>Tue, 07 Jul 2026 17:45:00 GMT</pubDate>
  </item>
</channel></rss>"""

    cycle_calls = []
    messages = []
    monkeypatch.setattr(news_watch, "fetch_url", lambda url, user_agent: (200, rss))
    monkeypatch.setattr(news_watch.scheduled_demo_cycle, "run", lambda ctx: cycle_calls.append("cycle") or {})
    monkeypatch.setattr(news_watch, "deliver", lambda text, to="stdout": messages.append((text, to)))

    result = news_watch.run(DummyContext())

    assert result["new_item_count"] == 1
    assert result["hit_count"] == 0
    assert result["triggered_count"] == 0
    assert cycle_calls == []
    assert messages == []
    assert (tmp_path / "TEST-GAME.news_watch.json").exists()


def test_news_watch_trigger_invokes_scheduled_demo_cycle_and_alerts(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.deliver_to = "telegram:123"
    settings.event_trigger_cooldown_minutes = 45
    settings.event_trigger_daily_cap = 8
    settings.research_teams_path.write_text("""
news_watch:
  high_impact_patterns:
    - lineup change
teams:
  yankees:
    canonical_name: New York Yankees
    aliases: [Yankees]
    rss_feeds:
      - name: mlb_yankees
        url: https://example.com/yankees/rss
""")
    artifacts = {}

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

        def write_json(self, suffix, payload):
            artifacts[suffix] = payload
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    rss = b"""<?xml version="1.0"?>
<rss><channel>
  <item>
    <title>Yankees lineup change before first pitch</title>
    <link>https://example.com/yankees-lineup-change</link>
    <description>Confirmed lineup change for Yankees.</description>
    <pubDate>Tue, 07 Jul 2026 17:45:00 GMT</pubDate>
  </item>
</channel></rss>"""

    cycle_calls = []
    messages = []
    monkeypatch.setattr(news_watch, "fetch_url", lambda url, user_agent: (200, rss))
    monkeypatch.setattr(
        news_watch.scheduled_demo_cycle,
        "run",
        lambda ctx: cycle_calls.append(ctx.settings.execution_mode) or {"exit_code": 0},
    )
    monkeypatch.setattr(news_watch, "deliver", lambda text, to="stdout": messages.append((text, to)))

    result = news_watch.run(DummyContext())

    assert result["hit_count"] == 1
    assert result["triggered_count"] == 1
    assert result["trigger_decisions"][0]["decision"] == "fired"
    assert cycle_calls == ["paper"]
    assert messages == [("news trigger: New York Yankees — Yankees lineup change before first pitch", "telegram:123")]


def test_qual_validation_clamps_and_discards_bad_entries():
    raw = json.dumps([
        {
            "ticker": "KXQUAL1",
            "qual_prob": 1.20,
            "base_rate": 0.50,
            "confidence": 0.70,
            "rationale": "Cited signal.",
            "citation_urls": ["https://example.com/a"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {
                    "event": "Lineup boost holds",
                    "p_event": 0.50,
                    "p_outcome_given_event": 0.98,
                    "citations": ["https://example.com/a"],
                },
                {
                    "event": "Market baseline remains right",
                    "p_event": 0.50,
                    "p_outcome_given_event": 0.98,
                    "citations": ["https://example.com/a"],
                },
            ],
        },
        {
            "ticker": "KXQUAL2",
            "qual_prob": 0.60,
            "base_rate": 0.50,
            "confidence": 0.54,
            "rationale": "Too weak.",
            "citation_urls": ["https://example.com/a"],
            "scenarios": [
                {"event": "A", "p_event": 1.0, "p_outcome_given_event": 0.60, "citations": ["https://example.com/a"]},
            ],
        },
        {
            "ticker": "KXQUAL3",
            "qual_prob": 0.60,
            "base_rate": 0.50,
            "confidence": 0.80,
            "rationale": "Hallucinated ticker.",
            "citation_urls": ["https://example.com/a"],
            "scenarios": [
                {"event": "A", "p_event": 1.0, "p_outcome_given_event": 0.60, "citations": ["https://example.com/a"]},
            ],
        },
        {
            "ticker": "KXQUAL2",
            "qual_prob": 0.60,
            "base_rate": 0.50,
            "confidence": 0.80,
            "rationale": "Missing valid cite.",
            "citation_urls": ["https://evil.example/x"],
            "scenarios": [
                {"event": "A", "p_event": 1.0, "p_outcome_given_event": 0.60, "citations": ["https://evil.example/x"]},
            ],
        },
    ])

    result = qual_research_core.validate_qual_output(
        raw,
        allowed_tickers={"KXQUAL1", "KXQUAL2"},
        allowed_urls={"https://example.com/a"},
        allowed_news_item_ids={"news-a"},
        url_to_news_item_ids={"https://example.com/a": ["news-a"]},
        model_run_id="run-1",
        created_at="2026-07-07T00:00:00+00:00",
    )

    assert len(result.accepted) == 1
    assert result.accepted[0]["ticker"] == "KXQUAL1"
    assert result.accepted[0]["qual_prob"] == 0.98
    assert result.accepted[0]["base_rate"] == 0.50
    assert result.accepted[0]["news_item_ids_used"] == ["news-a"]
    assert result.accepted[0]["analysis"]["prompt_version"] == qual_research_core.QUAL_PROMPT_VERSION
    assert sum(
        branch["p_event"] * branch["p_outcome_given_event"]
        for branch in result.accepted[0]["scenarios"]
    ) == pytest.approx(result.accepted[0]["qual_prob"])
    assert {row["reason"] for row in result.discarded} == {
        "confidence below 0.55",
        "ticker not in provided set",
        "no valid citation_urls",
    }


def test_qual_model_malformed_retry_then_unavailable():
    calls = []

    def invoker(command, prompt, timeout_seconds):
        calls.append(prompt)
        return True, "not json", ""

    result = qual_research_core.run_qual_model(
        command="/fake/codex exec",
        news_items=[{"url": "https://example.com/a", "title": "Yankees", "summary": "news"}],
        markets=[{"ticker": "KXQUAL1", "title": "Yankees win"}],
        timeout_seconds=1,
        invoker=invoker,
    )

    assert len(calls) == 2
    assert result["status"] == "unavailable"
    assert result["reason"].startswith("malformed-json-after-retry")


def test_qual_model_retries_non_reconciling_scenario_tree():
    calls = []
    bad = json.dumps([
        {
            "ticker": "KXQUAL1",
            "base_rate": 0.50,
            "qual_prob": 0.60,
            "confidence": 0.70,
            "rationale": "Bad tree.",
            "citation_urls": ["https://example.com/a"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {"event": "A", "p_event": 0.60, "p_outcome_given_event": 0.60, "citations": ["https://example.com/a"]},
                {"event": "B", "p_event": 0.60, "p_outcome_given_event": 0.60, "citations": ["https://example.com/a"]},
            ],
        }
    ])
    good = json.dumps([
        {
            "ticker": "KXQUAL1",
            "base_rate": 0.50,
            "qual_prob": 0.60,
            "confidence": 0.70,
            "rationale": "Good tree.",
            "citation_urls": ["https://example.com/a"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {"event": "A", "p_event": 0.50, "p_outcome_given_event": 0.70, "citations": ["https://example.com/a"]},
                {"event": "B", "p_event": 0.50, "p_outcome_given_event": 0.50, "citations": ["https://example.com/a"]},
            ],
        }
    ])

    def invoker(command, prompt, timeout_seconds):
        calls.append(prompt)
        return True, bad if len(calls) == 1 else good, ""

    result = qual_research_core.run_qual_model(
        command="/fake/codex exec",
        news_items=[{
            "id": "news-a",
            "url": "https://example.com/a",
            "title": "Yankees",
            "summary": "news",
        }],
        markets=[{"ticker": "KXQUAL1", "title": "Yankees win"}],
        timeout_seconds=1,
        invoker=invoker,
    )

    assert len(calls) == 2
    assert "Previous output was invalid" in calls[1]
    assert result["status"] == "ok"
    assert result["attempts"] == 2
    assert result["signals"][0]["qual_prob"] == pytest.approx(0.60)


def test_qual_model_cli_failure_is_unavailable():
    def invoker(command, prompt, timeout_seconds):
        return False, "", "command not found"

    result = qual_research_core.run_qual_model(
        command="/missing/codex exec",
        news_items=[{"url": "https://example.com/a", "title": "Yankees", "summary": "news"}],
        markets=[{"ticker": "KXQUAL1", "title": "Yankees win"}],
        timeout_seconds=1,
        invoker=invoker,
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "command not found"


def test_qual_model_primary_unavailable_uses_claude_when_key_set():
    calls = {"codex": 0, "claude": 0}
    raw = json.dumps([
        {
            "ticker": "KXQUAL1",
            "base_rate": 0.50,
            "qual_prob": 0.60,
            "confidence": 0.70,
            "rationale": "Claude fallback.",
            "citation_urls": ["https://example.com/a"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {"event": "A", "p_event": 0.50, "p_outcome_given_event": 0.70, "citations": ["https://example.com/a"]},
                {"event": "B", "p_event": 0.50, "p_outcome_given_event": 0.50, "citations": ["https://example.com/a"]},
            ],
        }
    ])

    def invoker(command, prompt, timeout_seconds):
        calls["codex"] += 1
        return False, "", "usage-limit"

    def claude_invoker(prompt, schema, model):
        calls["claude"] += 1
        assert schema == qual_research_core.QUAL_SIGNAL_ARRAY_SCHEMA
        assert model == "claude-test"
        return True, raw, ""

    result = qual_research_core.run_qual_model(
        command="/fake/codex exec",
        news_items=[{"id": "news-a", "url": "https://example.com/a", "title": "Yankees", "summary": "news"}],
        markets=[{"ticker": "KXQUAL1", "title": "Yankees win"}],
        timeout_seconds=1,
        invoker=invoker,
        claude_invoker=claude_invoker,
        fallback_model="claude-test",
        anthropic_api_key="test-key",
    )

    assert calls == {"codex": 1, "claude": 1}
    assert result["status"] == "ok"
    assert result["engine"] == "claude"
    assert result["signals"][0]["engine"] == "claude"


def test_qual_model_empty_key_keeps_primary_unavailable_behavior():
    called = {"claude": False}

    def invoker(command, prompt, timeout_seconds):
        return False, "", "usage-limit"

    def claude_invoker(prompt, schema, model):
        called["claude"] = True
        return True, "[]", ""

    result = qual_research_core.run_qual_model(
        command="/fake/codex exec",
        news_items=[{"id": "news-a", "url": "https://example.com/a", "title": "Yankees", "summary": "news"}],
        markets=[{"ticker": "KXQUAL1", "title": "Yankees win"}],
        timeout_seconds=1,
        invoker=invoker,
        claude_invoker=claude_invoker,
        anthropic_api_key="",
    )

    assert result["status"] == "unavailable"
    assert result["engine"] == "codex"
    assert called["claude"] is False


def test_near_miss_investigation_justified_output_validates():
    candidate = {
        "ticker": "KXEDGE",
        "candidate_id": "mlb:yankees:redsox",
        "model_prob": 0.529,
        "net_edge": 0.04463,
        "required_edge": 0.055,
        "near_miss_gap": 0.01037,
        "executable_price": {"price_cents": 48, "price_prob": 0.48},
    }
    raw = json.dumps({
        "justified": True,
        "direction_consistent": True,
        "confidence": 0.67,
        "rationale": "Specific cited lineup news supports the same side.",
        "citation_urls": ["https://example.com/a"],
        "news_item_ids_used": ["news-a"],
    })

    result = qual_research_core.validate_near_miss_output(
        raw,
        allowed_urls={"https://example.com/a"},
        allowed_news_item_ids={"news-a"},
        url_to_news_item_ids={"https://example.com/a": ["news-a"]},
        news_by_id={"news-a": {"id": "news-a", "source": "mlb_rss", "published_at": "2026-07-07T12:00:00+00:00"}},
        candidate=candidate,
        model_run_id="near-1",
        engine="claude",
    )

    assert result["signal_source"] == "qual_activated_quant"
    assert result["justified"] is True
    assert result["direction_consistent"] is True
    assert result["confidence"] == pytest.approx(0.67)
    assert result["news_item_ids"] == ["news-a"]
    assert result["engine"] == "claude"


def test_market_basket_builds_targeted_team_game_markets():
    now = "2026-07-07T12:00:00+00:00"
    slate_payload = {
        "candidates": [
            {
                "candidate_id": "mlb:newyorkyankees:bostonredsox",
                "sport": "mlb",
                "away_team": "New York Yankees",
                "home_team": "Boston Red Sox",
                "kalshi_markets": [
                    {"ticker": "KXMLBGAME-26JUL07NYYBOS-NYY", "series_ticker": "KXMLBGAME", "title": "Yankees winner?"},
                    {"ticker": "KXMLBSPREAD-26JUL07NYYBOS-NYY", "series_ticker": "KXMLBSPREAD", "title": "Yankees run line?"},
                    {"ticker": "KXMLBTOTAL-26JUL07NYYBOS-9", "series_ticker": "KXMLBTOTAL", "title": "Total runs?"},
                    {"ticker": "KXMLBCORNERS-IGNORED", "series_ticker": "KXMLBCORNERS", "title": "Corners?"},
                ],
            }
        ],
    }
    matches = {"rows": [
        {
            "ticker": "KXMLBGAME-26JUL07NYYBOS-NYY",
            "candidate_id": "mlb:newyorkyankees:bostonredsox",
            "event_ticker": "KXMLBGAME-26JUL07NYYBOS",
            "series_ticker": "KXMLBGAME",
            "title": "Yankees winner?",
        }
    ]}

    basket = slate.build_market_basket(
        team="yankees",
        hit={
            "canonical_name": "New York Yankees",
            "content_hash": "news-a",
            "headline": "Yankees lineup change",
            "published_at": now,
        },
        slate=slate_payload,
        market_matches=matches,
        reason="fixture",
    )

    assert basket["empty"] is False
    assert basket["news_item_ids"] == ["news-a"]
    assert "mlb:newyorkyankees:bostonredsox" in basket["event_ids"]
    assert basket["tickers"] == [
        "KXMLBGAME-26JUL07NYYBOS-NYY",
        "KXMLBSPREAD-26JUL07NYYBOS-NYY",
        "KXMLBTOTAL-26JUL07NYYBOS-9",
    ]


def test_qual_signal_full_analysis_persists_round_trip(tmp_path):
    store = research.ResearchStore(tmp_path / "research.sqlite")
    signal = {
        "ticker": "KXQUAL1",
        "base_rate": 0.50,
        "qual_prob": 0.60,
        "confidence": 0.70,
        "rationale": "Good tree.",
        "citation_urls": ["https://example.com/a"],
        "news_item_ids_used": ["news-a"],
        "scenarios": [
            {"event": "A", "p_event": 0.50, "p_outcome_given_event": 0.70, "citations": ["https://example.com/a"]},
            {"event": "B", "p_event": 0.50, "p_outcome_given_event": 0.50, "citations": ["https://example.com/a"]},
        ],
        "analysis": {
            "ticker": "KXQUAL1",
            "base_rate": 0.50,
            "qual_prob": 0.60,
            "confidence": 0.70,
            "rationale": "Good tree.",
            "citation_urls": ["https://example.com/a"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {"event": "A", "p_event": 0.50, "p_outcome_given_event": 0.70, "citations": ["https://example.com/a"]},
                {"event": "B", "p_event": 0.50, "p_outcome_given_event": 0.50, "citations": ["https://example.com/a"]},
            ],
            "model_run_id": "run-1",
            "prompt_version": qual_research_core.QUAL_PROMPT_VERSION,
        },
        "created_at": "2026-07-07T00:00:00+00:00",
        "model_run_id": "run-1",
        "prompt_version": qual_research_core.QUAL_PROMPT_VERSION,
        "signal_source": "qual",
    }

    assert store.record_qual_signals([signal]) == 1
    latest = store.latest_qual_signals(max_age_hours=100000, tickers=["KXQUAL1"])
    row = latest["KXQUAL1"]

    assert row["id"] >= 1
    assert row["base_rate"] == pytest.approx(0.50)
    assert row["scenarios"][0]["event"] == "A"
    assert row["analysis"]["prompt_version"] == qual_research_core.QUAL_PROMPT_VERSION
    assert row["news_item_ids_used"] == ["news-a"]


def test_qual_calibration_math_known_inputs():
    stats = qual_learning.qual_calibration_stats([
        {"confidence": 0.60, "prob": 0.60, "outcome": 1},
        {"confidence": 0.62, "prob": 0.70, "outcome": 0},
        {"confidence": 0.76, "prob": 0.80, "outcome": 1},
    ])
    bucket = next(row for row in stats if row["bucket"] == "0.55-0.65")
    lines = qual_learning.format_calibration_lines(stats)

    assert bucket["count"] == 2
    assert bucket["correct"] == 1
    assert bucket["correct_rate"] == pytest.approx(0.5)
    assert bucket["observed_rate"] == pytest.approx(0.5)
    assert bucket["mean_prob"] == pytest.approx(0.65)
    assert bucket["brier"] == pytest.approx(0.325)
    assert any("0.55-0.65" in line and "1/2" in line for line in lines)


def test_qual_prompt_injects_lessons_and_calibration_lines():
    prompt = qual_research_core.build_prompt(
        news_items=[{
            "id": "news-a",
            "team": "yankees",
            "title": "Yankees recap",
            "summary": "Bullpen held late.",
            "url": "https://example.com/a",
        }],
        markets=[{"ticker": "KXQUAL1", "teams": ["yankees"], "market_family": "mlb moneyline"}],
        lessons=[{
            "team": "yankees",
            "market_family": "mlb moneyline",
            "lesson": "Do not upgrade bullpen rest without late leverage evidence.",
            "evidence_cite": "bullpen held late",
            "hit_count": 2,
        }],
        calibration_lines=[
            "your 0.55-0.65 confidence signals have resolved correctly 1/2 times (50%), Brier 0.325"
        ],
    )

    assert "Do not upgrade bullpen rest without late leverage evidence." in prompt
    assert "your 0.55-0.65 confidence signals have resolved correctly 1/2 times" in prompt
    assert qual_research_core.QUAL_PROMPT_VERSION in prompt


def _broad_execution_artifacts(now, learning, *, validated):
    ticker = "KXMLBGAME-26JUL06NYYBOS-NYY"
    performance = performance_learner.family_performance(learning, "mlb moneyline")
    return {
        "market_snapshot.json": {
            "generated_at": now,
            "rows": [
                {
                    "ticker": ticker,
                    "captured_at": now,
                    "scenario_id": "BROAD-MLB-1",
                    "market": "mlb_moneyline",
                    "label": "Constructed broad-slate Yankees moneyline",
                    "side": "yes",
                    "entry_price_cents": 50,
                    "bid": 48,
                    "ask": 50,
                    "implied": 0.50,
                    "prior_p": 0.61,
                    "sgp_adjusted_prob": 0.61,
                    "risk": 3,
                }
            ],
        },
        "research_bundle.json": {
            "game_id": "TEST-GAME",
            "generated_at": now,
            "performance_learning": learning,
            "market_candidates": [
                {
                    "ticker": ticker,
                    "candidate_id": "mlb:newyorkyankees:bostonredsox",
                    "event_key": "mlb:2026-07-06:bostonredsox:newyorkyankees",
                    "broad_slate": True,
                    "scenario_id": "BROAD-MLB-1",
                    "market": "mlb_moneyline",
                    "trade_eligible": True,
                    "market_family": "mlb moneyline",
                    "performance": performance,
                    "validated": validated,
                    "market_type_verdict": "validated" if validated else "not_yet_validated",
                }
            ],
        },
    }


def test_risk_gate_rejects_core_failures(tmp_path):
    settings = _ExecSettings(tmp_path)
    assert risk.evaluate_trade_intent(_intent(stake_units=5.0), settings).approved
    assert not risk.evaluate_trade_intent(_intent(stake_units=5.01), settings).approved
    assert not risk.evaluate_trade_intent(_intent(edge=-0.10), settings).approved
    assert "missing SGP-adjusted probability" in risk.evaluate_trade_intent(
        _intent(sgp_adjusted_prob=None), settings).reasons
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert not risk.evaluate_trade_intent(_intent(captured_at=stale), settings).approved
    assert not risk.evaluate_trade_intent(_intent(risk=5, hope_bet=False), settings).approved
    settings.kill_switch_path.write_text("stop")
    assert not risk.evaluate_trade_intent(_intent(), settings).approved


def test_risk_gate_uses_demo_floor_for_paper_and_demo_only(tmp_path):
    settings = _ExecSettings(tmp_path)
    marginal = _intent(edge=0.04, model_prob=0.54, market_prob=0.50, sgp_adjusted_prob=0.54)

    settings.execution_mode = "paper"
    paper_decision = risk.evaluate_trade_intent(marginal, settings)
    assert paper_decision.approved
    assert any("meets min 0.030" in check.reason for check in paper_decision.checks)

    settings.execution_mode = "demo"
    demo_decision = risk.evaluate_trade_intent(marginal, settings)
    assert demo_decision.approved
    assert any("meets min 0.030" in check.reason for check in demo_decision.checks)

    settings.execution_mode = "live"
    live_decision = risk.evaluate_trade_intent(marginal, settings)
    assert not live_decision.approved
    assert any("below min 0.050" in reason for reason in live_decision.reasons)

    settings.execution_mode = "demo"
    below_demo = risk.evaluate_trade_intent(
        replace(marginal, edge=0.02, model_prob=0.52),
        settings,
    )
    assert not below_demo.approved
    assert any("below min 0.030" in reason for reason in below_demo.reasons)


def test_research_override_waives_only_minimum_edge(tmp_path):
    settings = _ExecSettings(tmp_path)
    intent = _intent(
        edge=0.01,
        stake_units=0.5,
        research_override=True,
        research_override_reason=(
            "Human-reviewed tactical research supports this selected outcome despite "
            "the modeled edge remaining below the automated minimum threshold."
        ),
        research_sources=(
            "https://example.com/source-one",
            "https://example.com/source-two",
        ),
        research_approved_by="ellingtonfagan",
    )

    missing_ack = risk.evaluate_trade_intent(intent, settings)
    assert not missing_ack.approved
    assert any("NBABOT_RESEARCH_OVERRIDE_ACK" in reason for reason in missing_ack.reasons)

    settings.research_override_ack = risk.RESEARCH_OVERRIDE_ACK
    approved = risk.evaluate_trade_intent(intent, settings)
    assert approved.approved
    assert any(check.name == "research_override" and check.passed
               for check in approved.checks)

    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert not risk.evaluate_trade_intent(
        replace(intent, captured_at=stale),
        settings,
    ).approved
    assert not risk.evaluate_trade_intent(
        replace(intent, stake_units=1.1),
        settings,
    ).approved


def test_research_override_execution_is_audited(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.research_override_ack = risk.RESEARCH_OVERRIDE_ACK
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent(
        edge=0.01,
        stake_units=0.5,
        research_override=True,
        research_override_reason=(
            "Human-reviewed tactical research supports this selected outcome despite "
            "the modeled edge remaining below the automated minimum threshold."
        ),
        research_sources=(
            "https://example.com/source-one",
            "https://example.com/source-two",
        ),
        research_approved_by="ellingtonfagan",
    )
    decision = risk.evaluate_trade_intent(intent, settings)

    receipt = execution.execute_paper(intent, decision, settings, store, audit)
    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]

    assert receipt.status == "filled"
    override = next(
        record for record in records
        if record["type"] == "RESEARCH_OVERRIDE_USED"
    )
    assert override["approved_by"] == "ellingtonfagan"
    assert len(override["sources"]) == 2
    assert override["edge"] == 0.01


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


def test_settlement_audit_records_paper_outcome_and_clv(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent(
        ticker="KXSETTLED",
        price_cents=40,
        bid_cents=38,
        ask_cents=40,
        market_prob=0.40,
        model_prob=0.60,
    )
    decision = risk.evaluate_trade_intent(intent, settings)
    receipt = execution.execute_paper(intent, decision, settings, store, audit)
    ranker_payload = {
        "game_id": settings.game_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {
                "ticker": "KXSETTLED",
                "consensus": {
                    "fair_prob": 0.55,
                    "book_count": 3,
                    "sources": ["pinnacle", "circa", "draftkings"],
                },
            }
        ],
    }
    settings.data_path("candidate_ranker.json").write_text(json.dumps(ranker_payload))

    class DummyKalshi:
        def market(self, ticker):
            assert ticker == "KXSETTLED"
            return {
                "ticker": ticker,
                "status": "settled",
                "result": "yes",
                "settled_at": "2026-07-06T12:00:00+00:00",
            }

    class DummyContext:
        def __init__(self):
            self.settings = settings
            self.kalshi = DummyKalshi()

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(settlement_audit, "deliver", lambda *args, **kwargs: None)

    result = settlement_audit.run(DummyContext())
    rows = store.latest_rows("settlement_records", 5)
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]

    assert receipt.status == "filled"
    assert result["summary"]["settled_count"] == 1
    assert result["summary"]["clv_beat_rate"] == 1.0
    assert result["records"][0]["outcome"] == 1
    assert result["records"][0]["closing_consensus_cents"] == 55.0
    assert result["records"][0]["clv_cents"] == 15.0
    assert result["records"][0]["brier_entry_model"] == pytest.approx(0.16)
    assert rows[0]["client_order_id"] == receipt.client_order_id
    assert rows[0]["winning_side"] == "yes"
    assert rows[0]["beat_closing_consensus"] == 1
    assert any(row["type"] == settlement_audit_core.SETTLEMENT_EVENT_TYPE
               for row in audit_rows)
    assert (tmp_path / "TEST-GAME.settlement_audit.json").exists()


def test_qual_signal_source_persists_through_order_and_settlement_without_fake_clv(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent(
        ticker="KXQUALSETTLED",
        price_cents=40,
        bid_cents=38,
        ask_cents=40,
        market_prob=0.40,
        model_prob=0.60,
        sgp_adjusted_prob=0.60,
        signal_source="qual",
        market_family="mlb moneyline",
    )
    decision = risk.evaluate_trade_intent(intent, settings)
    receipt = execution.execute_paper(intent, decision, settings, store, audit)
    ranker_payload = {
        "game_id": settings.game_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {
                "ticker": "KXQUALSETTLED",
                "signal_source": "qual",
                "model_prob": 0.60,
                "consensus": {"fair_prob": None, "book_count": 0, "sources": []},
            }
        ],
    }
    settings.data_path("candidate_ranker.json").write_text(json.dumps(ranker_payload))

    class DummyKalshi:
        def market(self, ticker):
            return {
                "ticker": ticker,
                "status": "settled",
                "result": "yes",
                "settled_at": "2026-07-06T12:00:00+00:00",
            }

    class DummyContext:
        def __init__(self):
            self.settings = settings
            self.kalshi = DummyKalshi()

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(settlement_audit, "deliver", lambda *args, **kwargs: None)

    result = settlement_audit.run(DummyContext())
    order_rows = store.latest_rows("paper_orders", 1)
    settlements = store.latest_rows("settlement_records", 1)

    assert receipt.status == "filled"
    assert json.loads(order_rows[0]["intent_json"])["signal_source"] == "qual"
    assert order_rows[0]["signal_source"] == "qual"
    assert result["records"][0]["signal_source"] == "qual"
    assert result["records"][0]["closing_consensus_prob"] is None
    assert result["records"][0]["clv_cents"] is None
    assert settlements[0]["signal_source"] == "qual"


def test_confluence_verdict_persists_through_order_audit_and_settlement(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent(
        ticker="KXCONFLUENCE",
        price_cents=40,
        bid_cents=38,
        ask_cents=40,
        market_prob=0.40,
        model_prob=0.60,
        sgp_adjusted_prob=0.60,
        confluence_verdict="agree",
        confluence={
            "consensus_fair_prob": 0.60,
            "qual_prob": 0.59,
            "qual_confidence": 0.70,
            "delta": -0.01,
            "verdict": "agree",
            "effective_base_min_edge": 0.02,
        },
        market_family="mlb moneyline",
    )
    decision = risk.evaluate_trade_intent(intent, settings)
    receipt = execution.execute_paper(intent, decision, settings, store, audit)
    settings.data_path("candidate_ranker.json").write_text(json.dumps({
        "game_id": settings.game_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": [
            {
                "ticker": "KXCONFLUENCE",
                "confluence_verdict": "agree",
                "consensus": {"fair_prob": 0.55, "book_count": 3, "sources": ["pinnacle"]},
            }
        ],
    }))

    class DummyKalshi:
        def market(self, ticker):
            return {
                "ticker": ticker,
                "status": "settled",
                "result": "yes",
                "settled_at": "2026-07-06T12:00:00+00:00",
            }

    class DummyContext:
        def __init__(self):
            self.settings = settings
            self.kalshi = DummyKalshi()

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(settlement_audit, "deliver", lambda *args, **kwargs: None)

    result = settlement_audit.run(DummyContext())
    order_rows = store.latest_rows("paper_orders", 1)
    settlements = store.latest_rows("settlement_records", 1)
    audit_rows = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]

    assert receipt.status == "filled"
    assert order_rows[0]["confluence_verdict"] == "agree"
    assert json.loads(order_rows[0]["intent_json"])["confluence_verdict"] == "agree"
    assert result["records"][0]["confluence_verdict"] == "agree"
    assert settlements[0]["confluence_verdict"] == "agree"
    assert any(
        row["type"] == "PAPER_ORDER" and row["confluence_verdict"] == "agree"
        for row in audit_rows
    )


def _scenario_signal(ticker="KXMLBGAME-26JUL06NYYBOS-NYY", *, model_run_id="qual-run"):
    return {
        "ticker": ticker,
        "base_rate": 0.50,
        "qual_prob": 0.60,
        "confidence": 0.70,
        "rationale": "Yankees bullpen is rested.",
        "citation_urls": ["https://example.com/yankees-news"],
        "news_item_ids_used": ["news-a"],
        "scenarios": [
            {
                "event": "Starter exits with lead",
                "p_event": 0.50,
                "p_outcome_given_event": 0.70,
                "citations": ["https://example.com/yankees-news"],
            },
            {
                "event": "Bullpen game stays close",
                "p_event": 0.50,
                "p_outcome_given_event": 0.50,
                "citations": ["https://example.com/yankees-news"],
            },
        ],
        "analysis": {
            "ticker": ticker,
            "base_rate": 0.50,
            "qual_prob": 0.60,
            "confidence": 0.70,
            "rationale": "Yankees bullpen is rested.",
            "citation_urls": ["https://example.com/yankees-news"],
            "news_item_ids_used": ["news-a"],
            "scenarios": [
                {
                    "event": "Starter exits with lead",
                    "p_event": 0.50,
                    "p_outcome_given_event": 0.70,
                    "citations": ["https://example.com/yankees-news"],
                },
                {
                    "event": "Bullpen game stays close",
                    "p_event": 0.50,
                    "p_outcome_given_event": 0.50,
                    "citations": ["https://example.com/yankees-news"],
                },
            ],
            "model_run_id": model_run_id,
            "prompt_version": qual_research_core.QUAL_PROMPT_VERSION,
            "market": {"teams": ["yankees"], "market_family": "mlb moneyline"},
        },
        "created_at": "2026-07-06T20:00:00+00:00",
        "model_run_id": model_run_id,
        "prompt_version": qual_research_core.QUAL_PROMPT_VERSION,
        "signal_source": "qual",
    }


def _recap_news_item():
    return {
        "id": "recap-news",
        "team": "yankees",
        "source": "mlb_yankees",
        "title": "Yankees beat Red Sox after late rally",
        "body": "New York Yankees beat Boston 5-4 after the bullpen held late.",
        "url": "https://example.com/yankees-recap",
        "published_at": "2026-07-07T05:00:00+00:00",
        "fetched_at": "2026-07-07T06:00:00+00:00",
        "content_hash": "recap-hash",
    }


def _postmortem_context(settings):
    class DummyContext:
        def __init__(self):
            self.settings = settings

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    return DummyContext()


def test_postmortem_strict_json_validation():
    raw = json.dumps({
        "which_scenario_occurred": 0,
        "event_prediction_grade": "The main branch occurred.",
        "conditional_grade": "Conditional probability was reasonable.",
        "what_was_missed": "Nothing major was missed.",
        "lessons": [
            {
                "lesson": "Treat bullpen rest as meaningful only when recap confirms late leverage usage.",
                "evidence_cite": "bullpen held late",
            }
        ],
    })

    parsed = qual_postmortem_core.validate_postmortem_output(raw, scenario_count=2)

    assert parsed["which_scenario_occurred"] == 0
    assert parsed["lessons"][0]["evidence_cite"] == "bullpen held late"
    with pytest.raises(ValueError, match="out of range"):
        qual_postmortem_core.validate_postmortem_output(
            json.dumps({**json.loads(raw), "which_scenario_occurred": 5}),
            scenario_count=2,
        )


def test_qual_postmortem_queues_on_cli_failure_then_retries(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.research_teams_path.write_text("""
teams:
  yankees:
    canonical_name: New York Yankees
    aliases: [New York Yankees, Yankees, NYY]
    rss_feeds:
      - name: mlb_yankees
        url: https://example.com/rss
""")
    store = research.ResearchStore(settings.research_db_path)
    store.record_qual_signals([_scenario_signal()])
    settlement = _settlement_fixture(
        1,
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        outcome=1,
        model_prob=0.60,
        market_prob=0.50,
    )
    settlement["signal_source"] = "qual"
    store.record_settlement(settlement)
    recap_item = _recap_news_item()

    monkeypatch.setattr(qual_postmortem, "deliver", lambda *args, **kwargs: None)
    monkeypatch.setattr(qual_postmortem, "fetch_recap_for_settlement", lambda **kwargs: {
        "recap_status": "found",
        "game_date": "2026-07-06",
        "team": "yankees",
        "source": "mlb_yankees",
        "item": recap_item,
        "source_status": [{"source": "mlb_yankees", "status": "ok", "matched": 1}],
    })
    monkeypatch.setattr(qual_postmortem, "run_postmortem_model", lambda **kwargs: {
        "status": "unavailable",
        "reason": "command not found",
        "postmortem": None,
    })

    first = qual_postmortem.run(_postmortem_context(settings))

    assert first["queued_count"] == 1
    assert store.latest_rows("qual_postmortems", 5) == []
    assert store.qual_recap_for_settlement("settled-1")["recap_status"] == "found"

    monkeypatch.setattr(qual_postmortem, "run_postmortem_model", lambda **kwargs: {
        "status": "ok",
        "postmortem": {
            "which_scenario_occurred": 0,
            "event_prediction_grade": "The branch structure matched the recap.",
            "conditional_grade": "The conditional was directionally reasonable.",
            "what_was_missed": "The analysis underweighted late bullpen leverage.",
            "lessons": [
                {
                    "lesson": "Do not upgrade a moneyline for bullpen rest unless the game context makes late leverage likely.",
                    "evidence_cite": "bullpen held late",
                }
            ],
            "created_at": "2026-07-07T10:00:00+00:00",
        },
    })

    second = qual_postmortem.run(_postmortem_context(settings))
    postmortems = store.latest_rows("qual_postmortems", 5)
    lessons = store.top_qual_lessons(teams=["yankees"], market_family="mlb moneyline", limit=5)

    assert second["completed_count"] == 1
    assert postmortems[0]["client_order_id"] == "settled-1"
    assert json.loads(postmortems[0]["postmortem_json"])["which_scenario_occurred"] == 0
    assert lessons[0]["hit_count"] == 1
    assert lessons[0]["evidence_cite"] == "bullpen held late"


def test_qual_postmortem_records_missing_recap_and_skips(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.research_teams_path.write_text("""
teams:
  yankees:
    canonical_name: New York Yankees
    aliases: [New York Yankees, Yankees, NYY]
    rss_feeds:
      - name: mlb_yankees
        url: https://example.com/rss
""")
    store = research.ResearchStore(settings.research_db_path)
    store.record_qual_signals([_scenario_signal()])
    settlement = _settlement_fixture(1, ticker="KXMLBGAME-26JUL06NYYBOS-NYY")
    settlement["signal_source"] = "qual"
    store.record_settlement(settlement)

    monkeypatch.setattr(qual_postmortem, "deliver", lambda *args, **kwargs: None)
    monkeypatch.setattr(qual_postmortem, "fetch_recap_for_settlement", lambda **kwargs: {
        "recap_status": "missing",
        "reason": "no-matching-recap",
        "game_date": "2026-07-06",
        "team_keys": ["yankees"],
        "source_status": [{"source": "mlb_yankees", "status": "ok", "matched": 0}],
    })

    result = qual_postmortem.run(_postmortem_context(settings))

    assert result["missing_recap_count"] == 1
    assert store.qual_recap_for_settlement("settled-1")["recap_status"] == "missing"
    assert store.latest_rows("qual_postmortems", 5) == []


def test_qual_lessons_dedup_and_hit_count(tmp_path):
    store = research.ResearchStore(tmp_path / "research.sqlite")

    assert store.upsert_qual_lesson(
        team="yankees",
        market_family="mlb moneyline",
        lesson_text="Check bullpen rest before upgrading a moneyline.",
        evidence_cite="first cite",
        postmortem_id=1,
    )
    assert store.upsert_qual_lesson(
        team="yankees",
        market_family="mlb moneyline",
        lesson_text="check bullpen rest before upgrading a moneyline!",
        evidence_cite="second cite",
        postmortem_id=2,
    )
    lessons = store.top_qual_lessons(teams=["yankees"], market_family="mlb moneyline", limit=5)

    assert len(lessons) == 1
    assert lessons[0]["hit_count"] == 2
    assert lessons[0]["evidence_cite"] == "second cite"


def test_performance_learner_does_not_validate_small_lucky_sample():
    records = [
        _settlement_fixture(
            i,
            outcome=1,
            model_prob=0.80,
            market_prob=0.40,
            beat_closing=True,
            clv_cents=8.0,
        )
        for i in range(20)
    ]

    payload = performance_learner.learn(records)
    row = payload["families"]["mlb moneyline"]

    assert row["settled_count"] == 20
    assert row["clv_beat_rate"] == 1.0
    assert row["brier_meaningfully_better_than_market"] is True
    assert row["validated"] is False
    assert row["market_type_verdict"] == "not_yet_validated"
    assert any("settled sample n=20" in item for item in row["validation_blockers"])


def test_performance_learner_validates_family_after_thresholds():
    records = [
        _settlement_fixture(
            i,
            outcome=1 if i < 80 else 0,
            model_prob=0.70,
            market_prob=0.50,
            beat_closing=i < 75,
            clv_cents=6.0 if i < 75 else -2.0,
        )
        for i in range(120)
    ]

    payload = performance_learner.learn(records)
    row = payload["families"]["mlb moneyline"]

    assert row["settled_count"] == 120
    assert row["clv_available_count"] == 120
    assert row["clv_beat_rate"] == pytest.approx(0.625)
    assert row["entry_model_brier_comparable"] == pytest.approx(0.223333)
    assert row["market_baseline_brier"] == pytest.approx(0.25)
    assert row["brier_improvement_vs_market"] == pytest.approx(0.026667)
    assert row["realized_vs_predicted_haircut"] == pytest.approx(0.952)
    assert row["validated"] is True
    assert row["market_type_verdict"] == "validated"


def test_performance_learner_separates_qual_from_consensus_families():
    consensus_rows = [
        _settlement_fixture(i, outcome=1, model_prob=0.70, market_prob=0.50)
        for i in range(2)
    ]
    qual_rows = [
        {
            **_settlement_fixture(100 + i, outcome=0, model_prob=0.40, market_prob=0.50),
            "signal_source": "qual",
        }
        for i in range(2)
    ]

    payload = performance_learner.learn(consensus_rows + qual_rows)

    assert "mlb moneyline" in payload["families"]
    assert "qual mlb moneyline" in payload["families"]
    assert payload["families"]["mlb moneyline"]["settled_count"] == 2
    assert payload["families"]["qual mlb moneyline"]["settled_count"] == 2


def test_performance_learner_separates_confluence_boosted_consensus_families():
    plain_rows = [
        _settlement_fixture(i, outcome=1, model_prob=0.70, market_prob=0.50)
        for i in range(2)
    ]
    confluence_rows = [
        {
            **_settlement_fixture(100 + i, outcome=1, model_prob=0.70, market_prob=0.50),
            "confluence_verdict": "agree",
        }
        for i in range(2)
    ]

    payload = performance_learner.learn(plain_rows + confluence_rows)

    assert "mlb moneyline" in payload["families"]
    assert "confluence_agree mlb moneyline" in payload["families"]
    assert payload["families"]["mlb moneyline"]["settled_count"] == 2
    assert payload["families"]["confluence_agree mlb moneyline"]["settled_count"] == 2


def test_broad_slate_daily_limit_ramps_with_validated_family_count(tmp_path):
    empty_store = research.ResearchStore(tmp_path / "empty.sqlite")
    empty_learning = performance_learner.learn_from_store(empty_store)

    assert risk.validated_market_family_count(empty_learning) == 0
    assert risk.broad_slate_daily_trade_limit(empty_learning) == 0

    store = research.ResearchStore(tmp_path / "validated.sqlite")
    _record_constructed_validated_mlb_family(store)
    learning = performance_learner.learn_from_store(store)

    assert risk.validated_market_family_count(learning) == 1
    assert risk.broad_slate_daily_trade_limit(learning) == 2


@pytest.mark.parametrize("mode", ["paper", "demo"])
def test_unvalidated_broad_slate_paper_demo_risk_allows_bootstrap(tmp_path, mode):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = mode
    intent = _intent(
        scenario_id="BROAD-BOOTSTRAP",
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        broad_slate=True,
        market_family="mlb moneyline",
        validated=False,
    )

    decision = risk.evaluate_trade_intent(
        intent,
        settings,
        risk.RiskContext(
            broad_slate_daily_trade_limit=0,
            paper_demo_broad_slate_trade_count=0,
            paper_demo_daily_trade_cap=50,
        ),
    )

    assert decision.approved
    assert any(
        check.name == "broad_slate_family_validation"
        and check.passed
        and "live mode" in check.reason
        for check in decision.checks
    )
    assert any(
        check.name == "paper_demo_broad_slate_daily_trade_cap" and check.passed
        for check in decision.checks
    )


def test_qual_daily_cap_blocks_at_configured_limit(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    intent = _intent(
        signal_source="qual",
        edge=0.08,
        model_prob=0.58,
        market_prob=0.50,
        sgp_adjusted_prob=0.58,
    )

    decision = risk.evaluate_trade_intent(
        intent,
        settings,
        risk.RiskContext(
            qual_daily_trade_count=10,
            qual_daily_trade_cap=10,
            paper_demo_daily_trade_cap=50,
        ),
    )

    assert not decision.approved
    assert any("qual daily trade cap 10 reached" in reason for reason in decision.reasons)


def test_live_execution_hard_blocks_qual_intents_even_when_gated(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"
    settings.dry_run = False
    settings.live_trading_ack = live_execute.LIVE_ACK
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    intent = _intent(signal_source="qual", edge=0.10, model_prob=0.60)
    decision = risk.evaluate_trade_intent(intent, settings)

    class DummyKalshi:
        def place_order(self, body):
            raise AssertionError("qual live placement must not be called")

    assert not decision.approved
    assert "qual-sourced intents are hard-blocked in live mode" in decision.reasons
    with pytest.raises(RuntimeError, match="qual-sourced intents"):
        execution.execute_live(intent, decision, settings, store, audit, DummyKalshi())

    qaq_intent = _intent(
        signal_source="qual_activated_quant",
        edge=0.04,
        net_edge=0.04,
        required_edge=0.035,
        model_prob=0.58,
    )
    qaq_decision = risk.evaluate_trade_intent(qaq_intent, settings)
    assert not qaq_decision.approved
    assert "qual-sourced intents are hard-blocked in live mode" in qaq_decision.reasons
    with pytest.raises(RuntimeError, match="qual-sourced intents"):
        execution.execute_live(qaq_intent, qaq_decision, settings, store, audit, DummyKalshi())


def test_performance_learner_suggests_shrink_only_overrides():
    shrinking_family = [
        _settlement_fixture(
            i,
            ticker="KXNFLSPREAD-26SEP01DALPHI-DAL3",
            outcome=1 if i < 60 else 0,
            model_prob=0.75,
            market_prob=0.50,
            beat_closing=True,
        )
        for i in range(120)
    ]
    above_predicted_family = [
        _settlement_fixture(
            500 + i,
            ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
            outcome=1 if i < 80 else 0,
            model_prob=0.50,
            market_prob=0.50,
            beat_closing=True,
        )
        for i in range(120)
    ]

    payload = performance_learner.learn(shrinking_family + above_predicted_family)
    overrides = performance_learner.suggest_overrides(payload)

    assert payload["families"]["nfl spread"]["realized_vs_predicted_haircut"] == pytest.approx(0.667)
    assert overrides["market_family_prior_multipliers"]["nfl spread"] == pytest.approx(0.95)
    assert "mlb moneyline" not in overrides["market_family_prior_multipliers"]


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
    assert kalshi.body["time_in_force"] == "good_till_canceled"
    assert kalshi.body["self_trade_prevention_type"] == "taker_at_cross"
    assert "action" not in kalshi.body
    assert "type" not in kalshi.body
    assert store.count_orders("demo_orders") == 1


def test_demo_execute_blocks_missing_demo_credentials_without_request(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    messages = []
    called = {"demo": False}

    class DummyKalshi:
        def demo_place_order(self, demo_api_base, body):
            called["demo"] = True
            return {"order": {"client_order_id": body["client_order_id"]}}

    class DummyContext:
        def __init__(self):
            self.settings = settings
            self.kalshi = DummyKalshi()

    def forbidden_refresh(ctx):
        raise AssertionError("demo-execute should block before refresh or network")

    monkeypatch.setattr(demo_execute, "refresh_research_for_execution", forbidden_refresh)
    monkeypatch.setattr(demo_execute, "deliver", lambda text, to="stdout": messages.append((text, to)))

    result = demo_execute.run(DummyContext())

    assert result["reason"] == "mode-blocked"
    assert "KALSHI_DEMO_API_KEY" in result["detail"]
    assert "KALSHI_DEMO_PRIVATE_KEY_PATH" in result["detail"]
    assert called["demo"] is False
    assert messages[0][0].startswith("[demo-execute] blocked:")


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


def test_live_broad_slate_requires_explicit_gate_with_constructed_validated_family(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"
    settings.dry_run = False
    settings.live_trading_ack = live_execute.LIVE_ACK
    settings.unit_usd = 10.0
    store = research.ResearchStore(settings.research_db_path)
    _record_constructed_validated_mlb_family(store)
    learning = performance_learner.learn_from_store(store)
    now = datetime.now(timezone.utc).isoformat()
    artifacts = _broad_execution_artifacts(now, learning, validated=True)
    messages = []
    called = {"live": False}

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    def forbidden_live_call(*args, **kwargs):
        called["live"] = True
        raise AssertionError("live order placement must not be reached")

    monkeypatch.setattr(live_execute, "refresh_research_for_execution", lambda ctx: artifacts["research_bundle.json"])
    monkeypatch.setattr(live_execute, "deliver", lambda text, to="stdout": messages.append((text, to)))
    monkeypatch.setattr(live_execute, "execute_live", forbidden_live_call)

    result = live_execute.run(DummyContext())

    assert risk.broad_slate_daily_trade_limit(learning) == 2
    assert result["reason"] == "mode-blocked"
    assert result["detail"] == (
        "set NBABOT_BROAD_SLATE_EXECUTION="
        f"{live_execute.BROAD_SLATE_ACK}"
    )
    assert called["live"] is False
    assert messages[0][0].startswith("[live-execute] blocked:")


def test_daily_portfolio_exposure_blocks_cross_game_order(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.max_daily_exposure_units = 5.0
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)

    first = _intent(
        game_id="MLB-GAME-1",
        scenario_id="MLB-1",
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        contracts=5,
        stake_units=2.5,
    )
    first_decision = risk.evaluate_trade_intent(first, settings)
    assert execution.execute_paper(first, first_decision, settings, store, audit).status == "filled"

    second = _intent(
        game_id="WNBA-GAME-2",
        scenario_id="WNBA-1",
        ticker="KXWNBAGAME-26JUL06CONNMIN-MIN",
        contracts=4,
        stake_units=2.0,
    )
    second_decision = risk.evaluate_trade_intent(
        second,
        settings,
        risk.RiskContext(
            portfolio_exposure_units=store.daily_order_exposure_units("paper_orders"),
        ),
    )
    assert execution.execute_paper(second, second_decision, settings, store, audit).status == "filled"

    third = _intent(
        game_id="NHL-GAME-3",
        scenario_id="NHL-1",
        ticker="KXNHLGAME-26OCT01NYRBOS-NYR",
        contracts=2,
        stake_units=1.0,
    )
    third_decision = risk.evaluate_trade_intent(
        third,
        settings,
        risk.RiskContext(
            game_exposure_units=store.game_order_exposure_units("paper_orders", third.game_id),
            portfolio_exposure_units=store.daily_order_exposure_units("paper_orders"),
        ),
    )
    receipt = execution.execute_paper(third, third_decision, settings, store, audit)

    assert store.game_order_exposure_units("paper_orders", third.game_id) == 0.0
    assert store.daily_order_exposure_units("paper_orders") == pytest.approx(4.5)
    assert store.count_orders("paper_orders") == 2
    assert receipt.status == "rejected"
    assert not third_decision.approved
    assert any("daily portfolio exposure 5.500" in reason for reason in third_decision.reasons)


def test_paper_broad_slate_bootstrap_records_unvalidated_family_with_zero_validated_families(
    tmp_path,
    monkeypatch,
):
    settings = _ExecSettings(tmp_path)
    settings.unit_usd = 10.0
    store = research.ResearchStore(settings.research_db_path)
    learning = performance_learner.learn_from_store(store)
    now = datetime.now(timezone.utc).isoformat()
    artifacts = _broad_execution_artifacts(now, learning, validated=False)
    messages = []

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(paper, "refresh_research_for_execution", lambda ctx: artifacts["research_bundle.json"])
    monkeypatch.setattr(paper, "deliver", lambda text, to="stdout": messages.append((text, to)))

    result = paper.run(DummyContext())
    order = result["orders"][0]

    assert risk.broad_slate_daily_trade_limit(learning) == 0
    assert order["receipt"]["status"] == "filled"
    assert order["decision"]["approved"] is True
    assert order["intent"]["broad_slate"] is True
    assert order["intent"]["validated"] is False
    assert store.count_orders("paper_orders") == 1


def test_live_zero_validated_families_blocks_broad_slate_even_with_broad_gate(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"
    settings.dry_run = False
    settings.live_trading_ack = live_execute.LIVE_ACK
    settings.broad_slate_execution = live_execute.BROAD_SLATE_ACK
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)
    learning = performance_learner.learn_from_store(store)
    intent = _intent(
        scenario_id="BROAD-MLB-1",
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        broad_slate=True,
        market_family="mlb moneyline",
        validated=False,
    )
    called = {"live": False}

    class DummyKalshi:
        def place_order(self, body):
            called["live"] = True
            return {"order_id": "should-not-run"}

    decision = risk.evaluate_trade_intent(
        intent,
        settings,
        risk.RiskContext(
            broad_slate_trade_count=0,
            broad_slate_daily_trade_limit=risk.broad_slate_daily_trade_limit(learning),
        ),
    )
    receipt = execution.execute_live(intent, decision, settings, store, audit, DummyKalshi())

    assert risk.broad_slate_daily_trade_limit(learning) == 0
    assert not decision.approved
    assert receipt.status == "rejected"
    assert called["live"] is False
    assert store.count_orders("live_orders") == 0
    assert any(
        "broad-slate market family mlb moneyline is not validated" in reason
        for reason in decision.reasons
    )
    assert any(
        "validated-family limit 0" in reason
        for reason in decision.reasons
    )


def test_paper_demo_broad_slate_daily_cap_blocks_after_shared_count(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.paper_demo_daily_trade_cap = 2
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)

    paper_intent = _intent(
        scenario_id="BROAD-PAPER-1",
        ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
        broad_slate=True,
        market_family="mlb moneyline",
        validated=False,
    )
    paper_decision = risk.evaluate_trade_intent(
        paper_intent,
        settings,
        risk.RiskContext(
            paper_demo_broad_slate_trade_count=0,
            paper_demo_daily_trade_cap=settings.paper_demo_daily_trade_cap,
        ),
    )
    assert execution.execute_paper(paper_intent, paper_decision, settings, store, audit).status == "filled"

    settings.execution_mode = "demo"
    demo_intent = _intent(
        scenario_id="BROAD-DEMO-1",
        ticker="KXMLBGAME-26JUL06NYYBOS-BOS",
        broad_slate=True,
        market_family="mlb moneyline",
        validated=False,
    )
    demo_decision = risk.evaluate_trade_intent(
        demo_intent,
        settings,
        risk.RiskContext(
            paper_demo_broad_slate_trade_count=store.daily_order_count(
                ("paper_orders", "demo_orders"),
                broad_slate_only=True,
            ),
            paper_demo_daily_trade_cap=settings.paper_demo_daily_trade_cap,
        ),
    )

    class DummyKalshi:
        def demo_place_order(self, demo_api_base, body):
            return {"order": {"client_order_id": body["client_order_id"]}}

    assert execution.execute_demo(
        demo_intent,
        demo_decision,
        settings,
        store,
        audit,
        DummyKalshi(),
    ).status == "submitted"
    assert store.daily_order_count(
        ("paper_orders", "demo_orders"),
        broad_slate_only=True,
    ) == 2

    settings.execution_mode = "paper"
    third_intent = _intent(
        scenario_id="BROAD-PAPER-2",
        ticker="KXMLBGAME-26JUL06NYYBOS-OVER",
        broad_slate=True,
        market_family="mlb moneyline",
        validated=False,
    )
    third_decision = risk.evaluate_trade_intent(
        third_intent,
        settings,
        risk.RiskContext(
            paper_demo_broad_slate_trade_count=store.daily_order_count(
                ("paper_orders", "demo_orders"),
                broad_slate_only=True,
            ),
            paper_demo_daily_trade_cap=settings.paper_demo_daily_trade_cap,
        ),
    )

    assert not third_decision.approved
    assert any(
        "paper/demo broad-slate daily cap 2 reached; order 3 blocked" in reason
        for reason in third_decision.reasons
    )


def test_backtest_rebuilds_s7_without_unresolved_leg(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scen, _, _ = scenarios.load_scenarios(_doc())
    log_path = tmp_path / "NBA-2026-FINALS-G3.log.jsonl"
    log_path.write_text((root / "data" / "NBA-2026-FINALS-G3.log.jsonl").read_text())

    metrics = backtesting.run_backtest("NBA-2026-FINALS-G3", log_path, scen)
    s7 = next(r for r in metrics["scenario_rows"] if r["scenario_id"] == "S7")

    assert s7["prior_p_joint"] == 0.10868
    assert metrics["scenario_count"] == 7


def test_ui_renders_broad_slate_dashboard_without_server(tmp_path):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    store.record_settlement(_settlement_fixture(0, ticker="KXSETTLED"))
    settings.data_path("candidate_ranker.json").write_text(json.dumps({
        "game_id": settings.game_id,
        "generated_at": "2026-07-07T12:00:00+00:00",
        "candidate_count": 2,
        "edge_pass_count": 1,
        "trade_eligible_count": 1,
        "rows": [
            {
                "ticker": "KXMLBGAME-26JUL07ATLPIT-PIT",
                "market_identity": {
                    "sport": "mlb",
                    "league": "MLB",
                    "market_type": "moneyline",
                    "participant": "pittsburghpirates",
                },
                "model_prob": 0.64,
                "edge": 0.08,
                "book_count": 3,
                "consensus": {
                    "book_count": 3,
                    "providers": ["parlay_api", "sportsgameodds"],
                },
                "executable_price": {
                    "bid_cents": 55,
                    "ask_cents": 56,
                    "spread_cents": 1,
                    "fillable_contracts": 1,
                    "vwap_cents": 56.0,
                    "captured_at": "2026-07-07T11:59:00+00:00",
                },
                "blockers": [],
                "passes_edge": True,
                "trade_eligible": True,
            },
            {
                "ticker": "KXMLBSPREAD-26JUL07ATLPIT-PIT2",
                "market_identity": {
                    "sport": "mlb",
                    "league": "MLB",
                    "market_type": "spread",
                    "participant": "pittsburghpirates",
                    "line": -1.5,
                },
                "model_prob": 0.90,
                "edge": 0.42,
                "book_count": 4,
                "consensus": {
                    "book_count": 4,
                    "providers": ["the_odds_api"],
                },
                "executable_price": {
                    "bid_cents": 47,
                    "ask_cents": 48,
                    "spread_cents": 1,
                    "fillable_contracts": 1,
                    "vwap_cents": 48.0,
                    "captured_at": "2026-07-07T11:59:00+00:00",
                },
                "blockers": [edge_engine.IMPLAUSIBLE_EDGE_BLOCKER],
                "passes_edge": False,
                "trade_eligible": False,
            },
        ],
    }))
    settings.data_path("research_bundle.json").write_text(json.dumps({
        "performance_learning": {
            "families": {
                "mlb moneyline": {
                    "settled_count": 20,
                    "clv_beat_rate": 0.65,
                    "validated": False,
                },
            },
        },
    }))
    settings.data_path("slate_candidates.json").write_text(json.dumps({
        "generated_at": "2026-07-07T11:58:00+00:00",
        "sports": ["mlb"],
        "provider_status": [
            {"provider": "parlay_api", "rows": 9, "ok": True, "x_requests_remaining": "984"},
            {"provider": "the_odds_api", "rows": 0, "ok": False,
             "skip_reason": "credits-below-floor", "x_requests_remaining": "12"},
        ],
    }))
    settings.data_path("source_check.json").write_text(json.dumps({
        "providers": [
            {"key": "sportsgameodds", "configured": True, "probe": {"ok": True, "reason": "ok"}},
            {"key": "the_odds_api", "configured": True, "probe": {"ok": True, "reason": "ok"}},
        ],
    }))

    class DummyContext:
        pass

    DummyContext.settings = settings

    html = ui.render_dashboard(DummyContext())

    assert "Kalshi Broad-Slate Edge Engine" in html
    assert "live broad-slate capacity is 0" in html
    assert "KXMLBGAME-26JUL07ATLPIT-PIT" in html
    assert "trade_eligible" in html
    assert "flagged: implausible edge" in html
    assert "edge exceeds plausible maximum; likely identity" not in html
    assert "parlay_api" in html
    assert "credit-gated" in html
    assert "KXSETTLED" in html
    assert "Scenarios" not in html


def test_book_watch_captures_and_stores_orderbooks(tmp_path, monkeypatch):
    class DummySettings:
        game_id = "TEST-GAME"
        deliver_to = "stdout"
        research_db_path = tmp_path / "research.sqlite"
        orderbook_depth = 10
        book_watch_iterations = 1
        book_watch_interval_seconds = 0

        def data_path(self, suffix):
            return tmp_path / f"{self.game_id}.{suffix}"

    class DummyKalshi:
        def orderbook(self, ticker, depth=None):
            assert depth == 10
            return orderbook.OrderBook.from_snapshot(ticker, {
                "yes": [[68, 100]],
                "no": [[28, 50]],
            })

    class DummyContext:
        settings = DummySettings()
        kalshi = DummyKalshi()
        game_tag = "TESTTAG"

        def read_json(self, suffix):
            if suffix == "market_candidates.json":
                return {"rows": [{"ticker": "KXTEST"}]}
            return None

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(book_watch, "deliver", lambda *args, **kwargs: None)

    result = book_watch.run(DummyContext())
    rows = research.ResearchStore(DummySettings.research_db_path).latest_rows(
        "orderbook_snapshots", 5,
    )

    assert result["tickers"] == ["KXTEST"]
    assert result["snapshots"][0]["metrics"]["KXTEST"]["yes"]["best_ask_cents"] == 72
    assert len(rows) == 1
    assert rows[0]["ticker"] == "KXTEST"


def test_odds_refresh_marks_stale_and_fresh_artifacts(tmp_path):
    now = datetime.now(timezone.utc)

    class DummySettings(_ExecSettings):
        stale_market_seconds = 90

    class DummyContext:
        settings = DummySettings(tmp_path)
        payload = {
            "generated_at": (now - timedelta(minutes=5)).isoformat(),
        }

        def read_json(self, suffix):
            return self.payload

    ctx = DummyContext()
    stale_report = odds_refresh.freshness_report(ctx, "slate_candidates.json")
    assert stale_report["fresh"] is False
    assert stale_report["reason"] == "stale > 90s"

    ctx.payload = {"generated_at": now.isoformat()}
    fresh_report = odds_refresh.freshness_report(ctx, "slate_candidates.json")
    assert fresh_report["fresh"] is True


def test_market_identity_exact_match_handles_team_order_and_totals():
    candidate = {
        "candidate_id": "soccer:spain:portugal",
        "sport": "soccer_world_cup",
        "league": "FIFA",
        "away_team": "Portugal",
        "home_team": "Spain",
        "start_time": "2026-07-07T20:00:00Z",
    }
    kalshi_identity = market_identity.resolve_kalshi_market(
        {"title": "Reg Time: Over 2.5 goals scored", "sport": "soccer_world_cup"},
        candidate,
    )
    line_identity = market_identity.resolve_line_market(
        {"market": "totals", "name": "Over", "point": 2.5, "price": -110},
        candidate,
    )

    matches = market_identity.match_identities(
        kalshi_identity,
        [{"candidate_id": candidate["candidate_id"], "line": {}, "identity": line_identity}],
    )

    assert matches[0]["match_type"] == "exact"
    assert kalshi_identity.event_key == line_identity.event_key


def test_market_identity_resolves_open_kalshi_title_to_event_key():
    identity = market_identity.resolve_kalshi_market(
        {
            "title": "Philadelphia Phillies vs Kansas City Royals, Over 8.5 runs scored",
            "sport": "mlb",
            "close_time": "2026-07-06T23:00:00Z",
        },
        {},
    )

    assert identity.event_key == "mlb:2026-07-06:kansascityroyals:philadelphiaphillies"
    assert identity.market_type == "total"
    assert identity.line == 8.5


def test_market_identity_no_coverage_returns_no_match():
    kalshi_identity = market_identity.MarketIdentity(
        sport="soccer_world_cup",
        league=None,
        event_key="soccer_world_cup:2026-07-07:spain:portugal",
        market_type="total",
        line=2.5,
        side="over",
        start_date="2026-07-07",
    )
    other = market_identity.MarketIdentity(
        sport="soccer_world_cup",
        league=None,
        event_key="soccer_world_cup:2026-07-07:france:belgium",
        market_type="total",
        line=2.5,
        side="over",
        start_date="2026-07-07",
    )

    assert market_identity.match_identities(
        kalshi_identity,
        [{"candidate_id": "other", "identity": other}],
    ) == []


def test_market_identity_mlb_doubleheader_suffix_matches_original_event_date():
    kalshi_identity = market_identity.resolve_kalshi_market(
        {
            "ticker": "KXMLBSPREAD-26JUL071945MILSTLG2-MIL2",
            "event_ticker": "KXMLBSPREAD-26JUL071945MILSTLG2",
            "series_ticker": "KXMLBSPREAD",
            "title": "Milwaukee wins by over 1.5 runs?",
            "sport": "mlb",
            "strike_type": "greater",
            "floor_strike": 1.5,
            "close_time": "2026-07-10T23:45:00Z",
        },
        {},
    )
    candidate = {
        "candidate_id": "mlb:milwaukeebrewers:stlouiscardinals",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Milwaukee Brewers",
        "home_team": "St. Louis Cardinals",
        "start_time": "2026-07-07T23:45:00Z",
    }
    line_identity = market_identity.resolve_line_market(
        {"market": "spreads", "name": "Milwaukee Brewers", "point": -1.5, "price": -120},
        candidate,
    )

    matches = market_identity.match_identities(
        kalshi_identity,
        [{"candidate_id": candidate["candidate_id"], "identity": line_identity}],
    )

    assert kalshi_identity.event_key == "mlb:2026-07-07:milwaukeebrewers:stlouiscardinals"
    assert kalshi_identity.start_date == "2026-07-07"
    assert matches[0]["match_type"] == "exact"


@pytest.mark.parametrize("line_patch", [
    {"name": "Milwaukee Brewers", "point": -2.5},
    {"name": "St. Louis Cardinals", "point": -1.5},
])
def test_market_identity_mlb_doubleheader_rejects_different_line_or_side(line_patch):
    kalshi_identity = market_identity.resolve_kalshi_market(
        {
            "ticker": "KXMLBSPREAD-26JUL071945MILSTLG2-MIL2",
            "event_ticker": "KXMLBSPREAD-26JUL071945MILSTLG2",
            "series_ticker": "KXMLBSPREAD",
            "title": "Milwaukee wins by over 1.5 runs?",
            "sport": "mlb",
            "strike_type": "greater",
            "floor_strike": 1.5,
        },
        {},
    )
    candidate = {
        "candidate_id": "mlb:milwaukeebrewers:stlouiscardinals",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Milwaukee Brewers",
        "home_team": "St. Louis Cardinals",
        "start_time": "2026-07-07T23:45:00Z",
    }
    line = {"market": "spreads", "price": -120, **line_patch}
    line_identity = market_identity.resolve_line_market(line, candidate)

    assert not any(
        row["match_type"] == "exact"
        for row in market_identity.match_identities(
            kalshi_identity,
            [{"candidate_id": candidate["candidate_id"], "identity": line_identity}],
        )
    )


@pytest.mark.parametrize("candidate_patch", [
    {"home_team": "Chicago Cubs"},
    {"start_time": "2026-07-08T23:45:00Z"},
])
def test_market_identity_mlb_doubleheader_rejects_different_team_or_date(candidate_patch):
    kalshi_identity = market_identity.resolve_kalshi_market(
        {
            "ticker": "KXMLBSPREAD-26JUL071945MILSTLG2-MIL2",
            "event_ticker": "KXMLBSPREAD-26JUL071945MILSTLG2",
            "series_ticker": "KXMLBSPREAD",
            "title": "Milwaukee wins by over 1.5 runs?",
            "sport": "mlb",
            "strike_type": "greater",
            "floor_strike": 1.5,
        },
        {},
    )
    candidate = {
        "candidate_id": "mlb:test",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Milwaukee Brewers",
        "home_team": "St. Louis Cardinals",
        "start_time": "2026-07-07T23:45:00Z",
        **candidate_patch,
    }
    line_identity = market_identity.resolve_line_market(
        {"market": "spreads", "name": "Milwaukee Brewers", "point": -1.5, "price": -120},
        candidate,
    )

    assert not any(
        row["match_type"] == "exact"
        for row in market_identity.match_identities(
            kalshi_identity,
            [{"candidate_id": candidate["candidate_id"], "identity": line_identity}],
        )
    )


def test_odds_math_devig_consensus_and_outlier_reporting():
    assert odds_math.implied_prob_american(-110) == pytest.approx(0.5238095)
    assert odds_math.implied_prob_decimal(2.0) == pytest.approx(0.5)
    assert odds_math.devig_multiplicative([
        odds_math.implied_prob_american(-110),
        odds_math.implied_prob_american(-110),
    ]) == pytest.approx([0.5, 0.5])

    three_way = odds_math.devig_shin([0.50, 0.30, 0.25])
    assert sum(three_way) == pytest.approx(1.0)
    assert sum(odds_math.devig_power([0.50, 0.30, 0.25])) == pytest.approx(1.0)

    survivors, excluded = odds_math.detect_outlier_books({
        "sharp_a": 0.50,
        "sharp_b": 0.51,
        "bad_line": 0.72,
    })
    assert "bad_line" in excluded
    assert set(survivors) == {"sharp_a", "sharp_b"}

    consensus = odds_math.consensus_prob([
        {"book": "pinnacle", "market": "totals", "name": "Over", "price": -110},
        {"book": "pinnacle", "market": "totals", "name": "Under", "price": -110},
        {"book": "circa", "market": "totals", "name": "Over", "price": -110},
        {"book": "circa", "market": "totals", "name": "Under", "price": -110},
    ], target_name="over")

    assert consensus["fair_prob"] == pytest.approx(0.5)
    assert consensus["book_count"] == 2
    assert consensus["disagreement_std"] == pytest.approx(0.0)


def test_consensus_dedupes_same_book_provider_collisions_by_freshest():
    consensus = odds_math.consensus_prob([
        {
            "provider": "the_odds_api",
            "book": "fanduel",
            "market": "totals",
            "name": "Over",
            "point": 8.5,
            "price": 900,
            "last_update": "2026-07-07T00:00:00Z",
        },
        {
            "provider": "the_odds_api",
            "book": "fanduel",
            "market": "totals",
            "name": "Under",
            "point": 8.5,
            "price": -10000,
            "last_update": "2026-07-07T00:00:00Z",
        },
        {
            "provider": "parlay_api",
            "book": "fanduel",
            "market": "totals",
            "name": "Over",
            "point": 8.5,
            "price": -110,
            "last_update": "2026-07-07T00:01:00Z",
        },
        {
            "provider": "parlay_api",
            "book": "fanduel",
            "market": "totals",
            "name": "Under",
            "point": 8.5,
            "price": -110,
            "last_update": "2026-07-07T00:01:00Z",
        },
        {"book": "pinnacle", "market": "totals", "name": "Over", "point": 8.5, "price": -110},
        {"book": "pinnacle", "market": "totals", "name": "Under", "point": 8.5, "price": -110},
    ], target_name="Over")

    assert consensus["fair_prob"] == pytest.approx(0.5)
    assert consensus["book_count"] == 2
    assert consensus["sources"] == ["fanduel", "pinnacle"]
    assert consensus["providers"] == ["parlay_api"]


def test_sportsgameodds_fixture_parser_extracts_teams_and_books():
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "fixtures" / "sportsgameodds_events_mlb_sample.json").read_text()
    )
    event = fixture["body"]["data"][0]

    away, home = slate._teams_from_event(event)
    markets = slate._line_markets_generic("sportsgameodds", event)

    assert away == "Philadelphia Phillies"
    assert home == "Kansas City Royals"
    assert markets
    assert any(row["book"] == "caesars" for row in markets)
    assert all(row.get("price") for row in markets[:10])
    assert any(row.get("name") in {"over", "under"} for row in markets)


def test_parlay_api_fixture_parser_converts_large_decimal_odds_and_excludes_kalshi():
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "fixtures" / "parlay_api_baseball_mlb_odds_decimal_sample.json").read_text()
    )
    price_format = fixture["request"]["params"]["oddsFormat"]
    event = next(
        row for row in fixture["body"]
        if row.get("away_team") == "Arizona Diamondbacks" and row.get("home_team") == "San Diego Padres"
    )
    markets = slate._line_markets_generic("parlay_api", event, price_format=price_format)
    padres = next(
        row for row in markets
        if row.get("book") == "bovada"
        and row.get("market") == "h2h"
        and row.get("name") == "San Diego Padres"
    )

    away, home = slate._teams_from_event(event)

    assert away == "Arizona Diamondbacks"
    assert home == "San Diego Padres"
    assert padres["price"] == 26.0
    assert padres["price_format"] == "decimal"
    assert odds_math.implied_prob(padres["price"], price_format=padres["price_format"]) == pytest.approx(1 / 26.0)
    assert all(row.get("book") != "kalshi" for row in markets)


def test_parlay_api_kalshi_bookmaker_is_not_a_consensus_source():
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "fixtures" / "parlay_api_baseball_mlb_odds_decimal_sample.json").read_text()
    )
    price_format = fixture["request"]["params"]["oddsFormat"]
    event = next(
        row for row in fixture["body"]
        if row.get("away_team") == "Chicago Cubs" and row.get("home_team") == "Baltimore Orioles"
    )
    markets = slate._line_markets_generic("parlay_api", event, price_format=price_format)
    h2h_rows = [row for row in markets if row.get("market") == "h2h"]
    consensus = odds_math.consensus_prob(h2h_rows, target_name="Chicago Cubs")

    assert any((book.get("key") or "").lower() == "kalshi" for book in event.get("bookmakers") or [])
    assert all(row.get("book") != "kalshi" for row in h2h_rows)
    assert "kalshi" not in consensus["sources"]
    assert "parlay_api" in consensus["providers"]


def test_edge_engine_dynamic_required_edge_and_blockers(tmp_path):
    settings = _ExecSettings(tmp_path)
    executable = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=48,
        ask_cents=50,
        spread_cents=2,
        fillable_contracts=10,
        vwap_cents=50,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    match = {"ticker": "KXEDGE", "match_type": "exact"}

    passed = edge_engine.evaluate_market(
        match,
        {
            "fair_prob": 0.62,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        },
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert passed.passes_edge is True

    thin = edge_engine.evaluate_market(
        match,
        {"fair_prob": 0.58, "book_count": 1, "sources": ["unknown"], "disagreement_std": 0.0},
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert thin.passes_edge is False
    assert "insufficient sportsbook consensus" in thin.blockers

    noisy = edge_engine.evaluate_market(
        match,
        {"fair_prob": 0.58, "book_count": 6, "sources": ["a", "b"], "disagreement_std": 0.10},
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert noisy.passes_edge is False
    assert "edge below dynamic required edge" in noisy.blockers

    fuzzy = edge_engine.evaluate_market(
        {"ticker": "KXEDGE", "match_type": "fuzzy"},
        {"fair_prob": 0.70, "book_count": 6, "sources": ["a", "b"], "disagreement_std": 0.0},
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert fuzzy.passes_edge is False
    assert "identity match is not exact" in fuzzy.blockers

    stale = edge_engine.evaluate_market(
        match,
        {"fair_prob": 0.70, "book_count": 6, "sources": ["a", "b"], "disagreement_std": 0.0},
        edge_engine.ExecutablePrice(
            side="yes",
            bid_cents=48,
            ask_cents=50,
            spread_cents=2,
            fillable_contracts=10,
            vwap_cents=50,
            captured_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        ),
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert "stale executable orderbook data" in stale.blockers


def test_demo_and_paper_edge_floor_passes_between_demo_and_live_thresholds(tmp_path):
    consensus = {
        "fair_prob": 0.553,
        "book_count": 6,
        "sources": ["pinnacle", "circa", "draftkings"],
        "disagreement_std": 0.0,
    }
    executable = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=48,
        ask_cents=50,
        spread_cents=2,
        fillable_contracts=10,
        vwap_cents=50,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )
    match = {"ticker": "KXEDGE", "match_type": "exact"}

    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    demo = edge_engine.evaluate_market(
        match,
        consensus,
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert demo.raw_edge == pytest.approx(0.053)
    assert demo.edge == pytest.approx(0.0355)
    assert demo.net_edge == pytest.approx(0.0355)
    assert demo.required_edge == pytest.approx(0.035)
    assert demo.passes_edge is True

    settings.execution_mode = "paper"
    paper = edge_engine.evaluate_market(
        match,
        consensus,
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert paper.required_edge == demo.required_edge
    assert paper.passes_edge is True

    settings.execution_mode = "live"
    settings.demo_min_edge = 0.0
    live = edge_engine.evaluate_market(
        match,
        consensus,
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )
    assert live.required_edge == pytest.approx(0.055)
    assert live.passes_edge is False
    assert "edge below dynamic required edge" in live.blockers


def test_demo_edge_floor_still_blocks_below_demo_threshold(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    executable = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=48,
        ask_cents=50,
        spread_cents=2,
        fillable_contracts=10,
        vwap_cents=50,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )

    result = edge_engine.evaluate_market(
        {"ticker": "KXEDGE", "match_type": "exact"},
        {
        "fair_prob": 0.552,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        },
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )

    assert result.raw_edge == pytest.approx(0.052)
    assert result.edge == pytest.approx(0.0345)
    assert result.required_edge == pytest.approx(0.035)
    assert result.passes_edge is False
    assert "edge below dynamic required edge" in result.blockers


def test_edge_engine_blocks_implausibly_large_edge(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.min_edge = 0.01
    settings.max_plausible_edge = 0.15
    match = {"ticker": "KXEDGE", "match_type": "exact"}
    consensus = {
        "fair_prob": 0.47,
        "book_count": 6,
        "sources": ["pinnacle", "circa", "draftkings"],
        "disagreement_std": 0.0,
    }
    cheap = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=1,
        ask_cents=2,
        spread_cents=1,
        fillable_contracts=10,
        vwap_cents=2,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )

    large = edge_engine.evaluate_market(
        match,
        consensus,
        cheap,
        settings,
        base_min_edge=settings.execution_min_edge,
    )

    assert large.raw_edge == pytest.approx(0.45)
    assert large.edge == pytest.approx(0.44863)
    assert large.passes_edge is False
    assert edge_engine.IMPLAUSIBLE_EDGE_BLOCKER in large.blockers

    normal = edge_engine.evaluate_market(
        match,
        {**consensus, "fair_prob": 0.56},
        edge_engine.ExecutablePrice(
            side="yes",
            bid_cents=48,
            ask_cents=50,
            spread_cents=2,
            fillable_contracts=10,
            vwap_cents=50,
            captured_at=datetime.now(timezone.utc).isoformat(),
        ),
        settings,
        base_min_edge=settings.execution_min_edge,
    )

    assert normal.raw_edge == pytest.approx(0.06)
    assert normal.edge == pytest.approx(0.0425)
    assert normal.passes_edge is True
    assert edge_engine.IMPLAUSIBLE_EDGE_BLOCKER not in normal.blockers


def test_edge_engine_plausible_guard_blocks_absurd_edge_in_demo(tmp_path):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    cheap = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=1,
        ask_cents=2,
        spread_cents=1,
        fillable_contracts=10,
        vwap_cents=2,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )

    result = edge_engine.evaluate_market(
        {"ticker": "KXEDGE", "match_type": "exact"},
        {
            "fair_prob": 0.47,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        },
        cheap,
        settings,
        base_min_edge=settings.execution_min_edge,
    )

    assert result.raw_edge == pytest.approx(0.45)
    assert result.edge == pytest.approx(0.44863)
    assert result.passes_edge is False
    assert edge_engine.IMPLAUSIBLE_EDGE_BLOCKER in result.blockers


def test_historical_backtest_lookahead_guard_filters_future_prices(tmp_path):
    decision = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)
    future = decision + timedelta(minutes=5)
    past = decision - timedelta(minutes=5)
    candles = [
        {
            "end_period_ts": int(past.timestamp()),
            "yes_bid": {"close": "0.38"},
            "yes_ask": {"close": "0.40"},
            "price": {"close": "0.39"},
        },
        {
            "end_period_ts": int(future.timestamp()),
            "yes_bid": {"close": "0.88"},
            "yes_ask": {"close": "0.90"},
            "price": {"close": "0.89"},
        },
    ]

    candle = backtest_replay.select_candlestick_at_or_before(candles, decision)
    metrics, quote = backtest_replay.executable_from_candlestick(candle, decision)

    assert quote["ask"] == 40
    assert metrics["yes"]["vwap_cents"] == 40

    odds_payload = {
        "timestamp": decision.isoformat(),
        "data": [
            {
                "id": "nyy-bos",
                "sport_key": "baseball_mlb",
                "commence_time": "2026-07-06T23:10:00Z",
                "away_team": "New York Yankees",
                "home_team": "Boston Red Sox",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "last_update": past.isoformat(),
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": past.isoformat(),
                                "outcomes": [
                                    {"name": "New York Yankees", "price": 150},
                                    {"name": "Boston Red Sox", "price": -160},
                                ],
                            }
                        ],
                    },
                    {
                        "key": "pinnacle",
                        "title": "Pinnacle",
                        "last_update": future.isoformat(),
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": future.isoformat(),
                                "outcomes": [
                                    {"name": "New York Yankees", "price": -900},
                                    {"name": "Boston Red Sox", "price": 700},
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    }
    slate_payload = backtest_replay.historical_odds_to_slate(
        odds_payload,
        odds_sport="baseball_mlb",
        decision_time=decision,
    )
    market_matches = {
        "rows": [
            {
                "ticker": "KXMLBGAME-26JUL06NYYBOS-NYY",
                "event_ticker": "KXMLBGAME-26JUL06NYYBOS",
                "series_ticker": "KXMLBGAME",
                "title": "New York Y vs Boston Winner?",
                "sport": "mlb",
                "kalshi_quote": quote,
                "orderbook": metrics,
                "close_time": "2026-07-06T23:10:00Z",
            }
        ]
    }

    class DummyContext:
        settings = _ExecSettings(tmp_path)

    result = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=decision,
    )
    row = result["rows"][0]

    assert slate_payload["filtered_future_rows"] == 1
    assert row["model_prob"] == pytest.approx(0.394, abs=0.01)
    assert row["model_prob"] < 0.50
    assert row["executable_price"]["vwap_cents"] == 40


def test_backtest_edge_records_do_not_feed_performance_validation(tmp_path):
    store = research.ResearchStore(tmp_path / "research.sqlite")
    store.record_backtest_edge_run(
        "run-1",
        "TEST-GAME",
        {
            "summary": {"record_count": 1},
            "api_cost": {"the_odds_api_credits_charged_observed": 10},
            "records": [
                {
                    "ticker": "KXMLBGAME-26JUL06NYYBOS-NYY",
                    "sport": "mlb",
                    "market_family": "mlb moneyline",
                    "decision_time": "2026-07-06T20:00:00Z",
                    "model_prob": 0.99,
                    "kalshi_price_prob": 0.50,
                    "edge": 0.49,
                    "outcome": 1,
                }
            ],
        },
    )

    learning = performance_learner.learn_from_store(store)

    assert learning["source"] == "settlement_records"
    assert learning["settled_count"] == 0
    assert learning["families"] == {}


def test_market_matcher_builds_orderbook_delta_execution_slate(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    previous_book = orderbook.OrderBook.from_snapshot("KXTEST", {
        "yes": [[50, 100]],
        "no": [[45, 50]],
    })
    current_book = orderbook.OrderBook.from_snapshot("KXTEST", {
        "yes": [[52, 125]],
        "no": [[46, 80]],
    })
    previous_metrics = {
        "KXTEST": {
            "yes": previous_book.metrics("yes"),
            "no": previous_book.metrics("no"),
        }
    }
    current_metrics = {
        "KXTEST": {
            "yes": current_book.metrics("yes"),
            "no": current_book.metrics("no"),
        }
    }
    store.record_orderbook_snapshots(settings.game_id, [previous_book], previous_metrics)
    store.record_orderbook_snapshots(settings.game_id, [current_book], current_metrics)

    slate_payload = {
        "generated_at": now,
        "candidates": [
            {
                "candidate_id": "kalshi:KXTEST",
                "sport": "soccer_world_cup",
                "sources": ["kalshi", "kalshi-open-markets"],
                "structured_sources": ["kalshi"],
                "kalshi_markets": [
                    {
                        "ticker": "KXTEST",
                        "title": "Spain advances, Reg Time: Over 2.5 goals scored",
                        "components": ["Spain advances", "Reg Time: Over 2.5 goals scored"],
                        "bid": 52,
                        "ask": 54,
                        "mid": 53,
                        "implied": 0.53,
                        "spread_cents": 2,
                        "captured_at": now,
                    }
                ],
            },
            {
                "candidate_id": "soccer:spain:portugal",
                "sport": "soccer_world_cup",
                "sources": ["the_odds_api"],
                "structured_sources": ["the_odds_api"],
                "away_team": "Spain",
                "home_team": "Portugal",
                "line_markets": [
                    {"provider": "the_odds_api", "market": "totals", "name": "Over", "point": 2.5}
                ],
            },
        ],
    }
    sport_handoff = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXTEST",
                "candidate_id": "kalshi:KXTEST",
                "sport": "soccer_world_cup",
                "title": "Spain advances, Reg Time: Over 2.5 goals scored",
                "bid": 52,
                "ask": 54,
                "implied": 0.53,
                "spread_cents": 2,
                "captured_at": now,
            }
        ],
    }
    book_payload = {
        "generated_at": now,
        "snapshots": [
            {
                "captured_at": now,
                "books": [current_book.as_dict()],
                "metrics": current_metrics,
            }
        ],
    }

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return {
                "slate_candidates.json": slate_payload,
                "sport_market_candidates.json": sport_handoff,
                "book_watch.json": book_payload,
            }.get(suffix)

    payload = market_matcher_core.build_market_matches(DummyContext())
    row = payload["rows"][0]

    assert payload["execution_review_count"] == 1
    assert payload["trade_eligible_count"] == 0
    assert row["execution_review"] is True
    assert row["trade_eligible"] is False
    assert "bid_rising" in row["orderbook_delta"]["signals"]
    assert "spread_tightening" in row["orderbook_delta"]["signals"]
    assert row["external_matches"]
    assert "candidate-ranker edge model required" in row["blockers"]


def test_market_matcher_phase_writes_execution_slate(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    settings = _ExecSettings(tmp_path)

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return {
                "slate_candidates.json": {"generated_at": now, "candidates": []},
                "sport_market_candidates.json": {"generated_at": now, "rows": []},
                "book_watch.json": {"generated_at": now, "snapshots": []},
            }.get(suffix)

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(market_matcher, "deliver", lambda *args, **kwargs: None)

    result = market_matcher.run(DummyContext())

    assert result["candidate_count"] == 0
    assert (tmp_path / "TEST-GAME.market_matches.json").exists()
    assert (tmp_path / "TEST-GAME.execution_slate.json").exists()


def _ranker_fixture(now: str):
    slate_payload = {
        "generated_at": now,
        "candidates": [
            {
                "candidate_id": "soccer:spain:portugal",
                "sport": "soccer_world_cup",
                "league": "FIFA",
                "away_team": "Spain",
                "home_team": "Portugal",
                "start_time": "2026-07-07T20:00:00Z",
                "line_markets": [
                    {"provider": "the_odds_api", "book": book, "market": "totals", "name": "Over", "point": 2.5, "price": -150}
                    for book in ("pinnacle", "circa", "draftkings", "fanduel", "betmgm", "caesars")
                ] + [
                    {"provider": "the_odds_api", "book": book, "market": "totals", "name": "Under", "point": 2.5, "price": 120}
                    for book in ("pinnacle", "circa", "draftkings", "fanduel", "betmgm", "caesars")
                ],
            }
        ],
    }
    market_matches = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXEDGE",
                "candidate_id": "soccer:spain:portugal",
                "sport": "soccer_world_cup",
                "title": "Reg Time: Over 2.5 goals scored",
                "close_time": "2026-07-07T20:00:00Z",
                "kalshi_quote": {
                    "bid": 48,
                    "ask": 50,
                    "mid": 49,
                    "implied": 0.49,
                    "spread_cents": 2,
                },
                "orderbook": {
                    "yes": {
                        "best_bid_cents": 48,
                        "best_ask_cents": 50,
                        "spread_cents": 2,
                        "fillable_contracts": 10,
                        "vwap_cents": 50,
                        "captured_at": now,
                    }
                },
                "orderbook_delta": {"signals": ["spread_tightening"]},
                "external_matches": [],
            }
        ],
    }
    return slate_payload, market_matches


def test_candidate_ranker_builds_edge_candidates(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    settings = _ExecSettings(tmp_path)
    slate_payload, market_matches = _ranker_fixture(now)

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return {
                "slate_candidates.json": slate_payload,
                "market_matches.json": market_matches,
                "book_watch.json": {"generated_at": now},
            }.get(suffix)

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(candidate_ranker, "deliver", lambda *args, **kwargs: None)

    result = candidate_ranker.run(DummyContext())
    row = result["rows"][0]

    assert result["candidate_count"] == 1
    assert result["edge_pass_count"] == 1
    assert result["diagnostics"]["candidates_with_line_markets"] == 1
    assert result["diagnostics"]["exact"] == 1
    assert result["diagnostics"]["kalshi_event_key_resolved"] == 1
    assert row["model_prob"] is not None
    assert row["edge"] > row["required_edge"]
    assert row["book_count"] == 6
    assert row["match_type"] == "exact"
    assert row["passes_edge"] is True
    assert (tmp_path / "TEST-GAME.candidate_ranker.json").exists()
    assert (tmp_path / "TEST-GAME.edge_candidates.json").exists()


def test_candidate_ranker_demo_floor_passes_marginal_edge_while_live_is_unchanged(tmp_path, monkeypatch):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.529,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)

    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

    demo_settings = _ExecSettings(tmp_path / "demo")
    demo_settings.execution_mode = "demo"
    demo = candidate_ranker_core.build_candidate_rankings(
        DummyContext(demo_settings),
        slate_payload,
        market_matches,
        now=now_dt,
    )
    demo_row = demo["rows"][0]
    assert demo["edge_pass_count"] == 1
    assert demo_row["raw_edge"] == pytest.approx(0.049)
    assert demo_row["edge"] == pytest.approx(0.04463)
    assert demo_row["required_edge"] == pytest.approx(0.035)
    assert demo_row["passes_edge"] is True

    live_settings = _ExecSettings(tmp_path / "live-a")
    live_settings.execution_mode = "live"
    live_settings.demo_min_edge = 0.0
    live = candidate_ranker_core.build_candidate_rankings(
        DummyContext(live_settings),
        slate_payload,
        market_matches,
        now=now_dt,
    )
    live_row = live["rows"][0]
    assert live["edge_pass_count"] == 0
    assert live_row["required_edge"] == pytest.approx(0.055)
    assert "edge below dynamic required edge" in live_row["blockers"]

    unchanged_live_settings = _ExecSettings(tmp_path / "live-b")
    unchanged_live_settings.execution_mode = "live"
    unchanged_live_settings.demo_min_edge = 0.03
    unchanged_live = candidate_ranker_core.build_candidate_rankings(
        DummyContext(unchanged_live_settings),
        slate_payload,
        market_matches,
        now=now_dt,
    )
    assert unchanged_live["rows"][0] == live_row


def _qual_ranker_fixture(now: str):
    slate_payload = {
        "generated_at": now,
        "candidates": [
            {
                "candidate_id": "kalshi:KXQUAL-NYY",
                "sport": "mlb",
                "structured_sources": ["kalshi"],
                "kalshi_markets": [
                    {
                        "ticker": "KXQUAL-NYY",
                        "title": "New York Yankees to win?",
                        "bid": 48,
                        "ask": 50,
                        "implied": 0.49,
                        "captured_at": now,
                    }
                ],
            }
        ],
    }
    market_matches = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXQUAL-NYY",
                "candidate_id": "kalshi:KXQUAL-NYY",
                "sport": "mlb",
                "title": "New York Yankees to win?",
                "kalshi_quote": {"bid": 48, "ask": 50, "mid": 49, "implied": 0.49, "spread_cents": 2},
                "orderbook": {
                    "yes": {
                        "best_bid_cents": 48,
                        "best_ask_cents": 50,
                        "spread_cents": 2,
                        "fillable_contracts": 10,
                        "vwap_cents": 50,
                        "captured_at": now,
                    }
                },
            }
        ],
    }
    return slate_payload, market_matches


def test_candidate_ranker_qual_prices_unconsensused_demo_rows_at_higher_floor(tmp_path):
    now_dt = datetime(2026, 7, 7, 18, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _qual_ranker_fixture(now)
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"

    class DummyContext:
        def __init__(self):
            self.settings = settings

    result = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXQUAL-NYY": {
                "ticker": "KXQUAL-NYY",
                "qual_prob": 0.57,
                "confidence": 0.70,
                "rationale": "Lineup and bullpen notes support the Yankees.",
                "citation_urls": ["https://example.com/yankees-lineup"],
                "created_at": now,
                "model_run_id": "qual-run",
            }
        },
    )
    row = result["rows"][0]

    assert row["signal_source"] == "qual"
    assert row["model_prob"] == pytest.approx(0.57)
    assert row["raw_edge"] == pytest.approx(0.09)
    assert row["edge"] == pytest.approx(0.08563)
    assert row["required_edge"] == pytest.approx(0.065)
    assert row["passes_edge"] is True
    assert row["trade_eligible"] is True
    assert row["sgp_adjusted_prob"] == pytest.approx(0.57)

    blocked = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXQUAL-NYY": {
                "ticker": "KXQUAL-NYY",
                "qual_prob": 0.549,
                "confidence": 0.70,
                "rationale": "Not enough edge.",
                "citation_urls": ["https://example.com/yankees-lineup"],
                "created_at": now,
                "model_run_id": "qual-run",
            }
        },
    )

    assert blocked["rows"][0]["signal_source"] == "qual"
    assert blocked["rows"][0]["passes_edge"] is False
    assert "edge below dynamic required edge" in blocked["rows"][0]["blockers"]


def test_candidate_ranker_near_miss_qaq_upgrade_lowers_floor_with_evidence(tmp_path, monkeypatch):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.518,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"

    class DummyContext:
        def __init__(self):
            self.settings = settings

    initial = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
    )
    near_misses = candidate_ranker_core.select_near_miss_candidates(initial["rows"], settings)
    assert len(near_misses) == 1
    assert near_misses[0]["near_miss_gap"] == pytest.approx(0.00137)

    upgraded = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qaq_verdicts={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "justified": True,
                "direction_consistent": True,
                "confidence": 0.70,
                "rationale": "Specific cited lineup news supports the quantitative side.",
                "citation_urls": ["https://example.com/a"],
                "news_item_ids": ["news-a"],
                "first_seen_at": now,
                "source": "mlb_rss",
                "engine": "codex",
            }
        },
    )
    row = upgraded["rows"][0]

    assert row["signal_source"] == "qual_activated_quant"
    assert row["raw_edge"] == pytest.approx(0.038)
    assert row["edge"] == pytest.approx(0.03363)
    assert row["required_edge"] == pytest.approx(0.02)
    assert row["passes_edge"] is True
    assert row["qaq_evidence"]["news_item_ids"] == ["news-a"]
    assert upgraded["signal_source_counts"]["qual_activated_quant"] == 1


def test_candidate_ranker_logs_qual_delta_but_keeps_consensus_source(tmp_path):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.58,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    class DummyContext:
        def __init__(self):
            self.settings = _ExecSettings(tmp_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)
        result = candidate_ranker_core.build_candidate_rankings(
            DummyContext(),
            slate_payload,
            market_matches,
            now=now_dt,
            qual_signals={
                "KXEDGE": {
                    "ticker": "KXEDGE",
                    "qual_prob": 0.62,
                    "confidence": 0.8,
                    "citation_urls": ["https://example.com/a"],
                }
            },
        )

    row = result["rows"][0]
    assert row["signal_source"] == "consensus"
    assert row["model_prob"] == pytest.approx(0.58)
    assert row["qual_vs_consensus_delta"] == pytest.approx(0.04)


def test_candidate_ranker_confluence_agreement_does_not_lower_demo_required_base(tmp_path, monkeypatch):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.519,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)

    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    agreed = candidate_ranker_core.build_candidate_rankings(
        DummyContext(settings),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.518,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )
    row = agreed["rows"][0]

    assert row["signal_source"] == "consensus"
    assert row["confluence_verdict"] == "agree"
    assert row["confluence"]["edge_bonus"] == pytest.approx(0.0)
    assert row["confluence"]["effective_base_min_edge"] == pytest.approx(0.03)
    assert row["raw_edge"] == pytest.approx(0.039)
    assert row["edge"] == pytest.approx(0.03463)
    assert row["required_edge"] == pytest.approx(0.035)
    assert row["passes_edge"] is False

    neutral = candidate_ranker_core.build_candidate_rankings(
        DummyContext(settings),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.60,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )
    neutral_row = neutral["rows"][0]
    assert neutral_row["confluence_verdict"] == "neutral"
    assert neutral_row["required_edge"] == pytest.approx(0.035)
    assert neutral_row["passes_edge"] is False

    settings.demo_min_edge = 0.025
    floored = candidate_ranker_core.build_candidate_rankings(
        DummyContext(settings),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.518,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )
    floor_row = floored["rows"][0]
    assert floor_row["confluence"]["edge_bonus"] == pytest.approx(0.0)
    assert floor_row["confluence"]["effective_base_min_edge"] == pytest.approx(0.025)
    assert floor_row["required_edge"] == pytest.approx(0.03)
    assert floor_row["passes_edge"] is True


def test_candidate_ranker_confluence_disagreement_vetoes_demo_and_records_shadow(tmp_path, monkeypatch):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.54,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)

    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    settings.unit_usd = 1.0

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

    result = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.45,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )
    rank_row = result["rows"][0]

    assert rank_row["raw_edge"] == pytest.approx(0.06)
    assert rank_row["edge"] == pytest.approx(0.05563)
    assert rank_row["required_edge"] == pytest.approx(0.035)
    assert rank_row["passes_edge"] is False
    assert rank_row["confluence_verdict"] == "disagree"
    assert rank_row["confluence_shadow"] is True
    assert confluence.VETO_REASON in rank_row["blockers"]

    artifacts = {
        "market_snapshot.json": {"generated_at": now, "rows": []},
        "slate_candidates.json": {
            "generated_at": now,
            "candidates": [
                {
                    "candidate_id": "kalshi:KXEDGE",
                    "sport": "soccer_world_cup",
                    "structured_sources": ["kalshi", "the_odds_api"],
                    "kalshi_markets": [
                        {
                            "ticker": "KXEDGE",
                            "title": "Reg Time: Over 2.5 goals scored",
                            "bid": 48,
                            "ask": 50,
                            "mid": 49,
                            "implied": 0.50,
                            "captured_at": now,
                        }
                    ],
                }
            ],
        },
        "slate_verification.json": {"verified_at": now, "rows": []},
        "book_watch.json": {
            "generated_at": now,
            "snapshots": [
                {
                    "captured_at": now,
                    "metrics": {
                        "KXEDGE": {
                            "yes": {
                                "captured_at": now,
                                "fillable_contracts": 10,
                                "spread_cents": 2,
                            }
                        }
                    },
                }
            ],
        },
        "market_matches.json": {
            "generated_at": now,
            "rows": [{"ticker": "KXEDGE", "captured_at": now, "freshness": {}}],
        },
        "candidate_ranker.json": {"generated_at": now, "rows": [rank_row]},
    }
    bundle = slate.research_bundle(
        DummyContext(),
        artifacts["slate_candidates.json"],
        artifacts["slate_verification.json"],
    )
    artifacts["research_bundle.json"] = bundle
    store = research.ResearchStore(settings.research_db_path)
    audit = __import__("nbabot.audit", fromlist=["AuditTrail"]).AuditTrail(tmp_path, store)

    inserted = paper.record_shadow_intents(DummyContext(), store, audit, mode="demo")
    shadows = store.list_shadow_intents(unsettled_only=False)

    assert inserted == 1
    assert bundle["market_candidates"][0]["trade_eligible"] is False
    assert bundle["market_candidates"][0]["confluence_shadow"] is True
    assert shadows[0]["ticker"] == "KXEDGE"
    assert shadows[0]["side"] == "yes"
    assert shadows[0]["contracts"] == pytest.approx(1.0)
    assert shadows[0]["price_cents"] == pytest.approx(48.0)
    assert shadows[0]["stake_units"] == pytest.approx(0.48)
    assert shadows[0]["edge"] == pytest.approx(0.05563)
    assert shadows[0]["reason"] == confluence.VETO_REASON
    assert shadows[0]["confluence_verdict"] == "disagree"


def test_candidate_ranker_confluence_does_not_apply_in_live_mode(tmp_path, monkeypatch):
    now_dt = datetime(2026, 7, 7, 19, 0, tzinfo=timezone.utc)
    now = now_dt.isoformat()
    slate_payload, market_matches = _ranker_fixture(now)

    def fixed_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.53,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", fixed_consensus)

    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "live"

    class DummyContext:
        def __init__(self):
            self.settings = settings

    result = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.525,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )
    row = result["rows"][0]

    assert row["confluence_verdict"] == "agree"
    assert row["confluence"]["edge_bonus"] == 0.0
    assert row["required_edge"] == pytest.approx(0.055)
    assert row["passes_edge"] is False

    boosted_live_intent = _intent(
        edge=0.025,
        model_prob=0.525,
        market_prob=0.50,
        sgp_adjusted_prob=0.525,
        confluence_verdict="agree",
        confluence={"effective_base_min_edge": 0.02},
    )
    decision = risk.evaluate_trade_intent(boosted_live_intent, settings)
    assert not decision.approved
    assert any("below min 0.050" in reason for reason in decision.reasons)

    def disagree_consensus(_rows, target_name=None):
        return {
            "fair_prob": 0.54,
            "book_count": 6,
            "sources": ["pinnacle", "circa", "draftkings"],
            "disagreement_std": 0.0,
        }

    monkeypatch.setattr(candidate_ranker_core, "consensus_prob", disagree_consensus)
    veto_candidate = candidate_ranker_core.build_candidate_rankings(
        DummyContext(),
        slate_payload,
        market_matches,
        now=now_dt,
        qual_signals={
            "KXEDGE": {
                "ticker": "KXEDGE",
                "qual_prob": 0.45,
                "confidence": 0.80,
                "citation_urls": ["https://example.com/a"],
            }
        },
    )["rows"][0]

    assert veto_candidate["confluence_verdict"] == "disagree"
    assert veto_candidate["confluence_shadow"] is False
    assert confluence.VETO_REASON not in veto_candidate["blockers"]


def test_candidate_ranker_records_closest_rejected_identity_for_unmatched_rows(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    candidate = {
        "candidate_id": "mlb:atlantabraves:pittsburghpirates",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Atlanta Braves",
        "home_team": "Pittsburgh Pirates",
        "start_time": "2026-07-07T18:40:00Z",
        "structured_sources": ["the_odds_api"],
        "line_markets": [
            {"provider": "the_odds_api", "book": "fanduel", "market": "totals", "name": "Over", "point": 8.5, "price": -110},
            {"provider": "the_odds_api", "book": "fanduel", "market": "totals", "name": "Under", "point": 8.5, "price": -110},
        ],
    }
    slate_payload = {"generated_at": now, "candidates": [candidate]}
    market_matches = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXMLBTOTAL-26JUL071840ATLPIT-10",
                "event_ticker": "KXMLBTOTAL-26JUL071840ATLPIT",
                "series_ticker": "KXMLBTOTAL",
                "candidate_id": "kalshi:KXMLBTOTAL-26JUL071840ATLPIT-10",
                "sport": "mlb",
                "title": "Atlanta vs Pittsburgh Total Runs?",
                "kalshi_quote": {"bid": 48, "ask": 50, "mid": 49, "implied": 0.49, "spread_cents": 2},
                "orderbook": {
                    "yes": {
                        "best_bid_cents": 48,
                        "best_ask_cents": 50,
                        "spread_cents": 2,
                        "fillable_contracts": 10,
                        "vwap_cents": 50,
                        "captured_at": now,
                    }
                },
            }
        ],
    }

    class DummyContext:
        def __init__(self):
            self.settings = _ExecSettings(tmp_path)

    result = candidate_ranker_core.build_candidate_rankings(DummyContext(), slate_payload, market_matches)
    row = result["rows"][0]
    coverage = result["diagnostics"]["match_coverage"]
    example = coverage["examples"][0]

    assert row["match_type"] == "fuzzy"
    assert row["passes_edge"] is False
    assert "identity match is not exact" in row["blockers"]
    assert coverage["unmatched_candidate_count"] == 1
    assert coverage["closest_rejection_field_counts"]["line"] == 1
    assert example["ticker"] == "KXMLBTOTAL-26JUL071840ATLPIT-10"
    assert example["differing_fields"] == ["line"]
    assert example["sportsbook_line"]["point"] == 8.5


def test_edge_engine_fuzzy_match_never_passes_even_with_large_edge(tmp_path):
    settings = _ExecSettings(tmp_path)
    executable = edge_engine.ExecutablePrice(
        side="yes",
        bid_cents=38,
        ask_cents=40,
        spread_cents=2,
        fillable_contracts=10,
        vwap_cents=40,
        captured_at=datetime.now(timezone.utc).isoformat(),
    )

    result = edge_engine.evaluate_market(
        {"ticker": "KXFUZZY", "match_type": "fuzzy"},
        {"fair_prob": 0.52, "book_count": 6, "sources": ["a", "b"], "disagreement_std": 0.0},
        executable,
        settings,
        base_min_edge=settings.execution_min_edge,
    )

    assert result.raw_edge == pytest.approx(0.12)
    assert result.edge == pytest.approx(0.1032)
    assert result.passes_edge is False
    assert "identity match is not exact" in result.blockers


def test_kalshi_mlb_spread_floor_strike_matches_same_outcome_line():
    real_kalshi_market = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "fixtures" / "kalshi_mlb_spread_market_sample.json").read_text()
    )
    candidate = {
        "candidate_id": "mlb:coloradorockies:losangelesdodgers",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Colorado Rockies",
        "home_team": "Los Angeles Dodgers",
        "start_time": "2026-07-07T02:10:00Z",
    }
    identity = market_identity.resolve_kalshi_market(real_kalshi_market, candidate)
    same_outcome = market_identity.resolve_line_market(
        {"provider": "fixture", "book": "fanduel", "market": "spreads", "name": "Colorado Rockies", "point": -1.5, "price": +920},
        candidate,
    )
    different_line = market_identity.resolve_line_market(
        {"provider": "fixture", "book": "fanduel", "market": "spreads", "name": "Colorado Rockies", "point": +1.5, "price": +105},
        candidate,
    )
    complement = market_identity.resolve_line_market(
        {"provider": "fixture", "book": "fanduel", "market": "spreads", "name": "Los Angeles Dodgers", "point": +1.5, "price": -2000},
        candidate,
    )

    assert identity.line == -1.5
    assert identity.side == "coloradorockies"
    assert market_identity.match_identities(identity, [{"candidate_id": candidate["candidate_id"], "identity": same_outcome}])[0]["match_type"] == "exact"
    assert not any(
        row["match_type"] == "exact"
        for row in market_identity.match_identities(
            identity,
            [{"candidate_id": candidate["candidate_id"], "identity": different_line}],
        )
    )
    assert candidate_ranker_core._same_consensus_market(identity, same_outcome)
    assert candidate_ranker_core._same_consensus_market(identity, complement)
    assert not candidate_ranker_core._same_consensus_market(identity, different_line)


def test_candidate_ranker_matches_kalshi_mve_legs_from_real_odds_fixture(tmp_path):
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "fixtures" / "the_odds_api_baseball_mlb_odds_sample.json").read_text()
    )
    event = next(
        row for row in fixture["body"]
        if row.get("away_team") == "Colorado Rockies" and row.get("home_team") == "Los Angeles Dodgers"
    )
    candidate = {
        "candidate_id": "mlb:coloradorockies:losangelesdodgers",
        "sport": "mlb",
        "league": "MLB",
        "away_team": event["away_team"],
        "home_team": event["home_team"],
        "start_time": event["commence_time"],
        "structured_sources": ["the_odds_api"],
        "line_markets": slate._line_markets_from_odds_api(event),
    }
    now = datetime.now(timezone.utc).isoformat()
    slate_payload = {"generated_at": now, "candidates": [candidate]}
    market_matches = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXMVE-TEST",
                "candidate_id": "kalshi:KXMVE-TEST",
                "sport": "mlb",
                "title": "yes Los Angeles D wins by over 1.5 runs,yes Over 9.5 runs scored",
                "mve_selected_legs": [
                    {
                        "event_ticker": "KXMLBSPREAD-26JUL062210COLLAD",
                        "market_ticker": "KXMLBSPREAD-26JUL062210COLLAD-LAD2",
                        "side": "yes",
                    },
                    {
                        "event_ticker": "KXMLBTOTAL-26JUL062210COLLAD",
                        "market_ticker": "KXMLBTOTAL-26JUL062210COLLAD-10",
                        "side": "yes",
                    },
                ],
                "kalshi_quote": {"bid": 20, "ask": 22, "mid": 21, "implied": 0.21, "spread_cents": 2},
                "orderbook": {
                    "yes": {
                        "best_bid_cents": 20,
                        "best_ask_cents": 22,
                        "spread_cents": 2,
                        "fillable_contracts": 10,
                        "vwap_cents": 22,
                        "captured_at": now,
                    }
                },
            }
        ],
    }

    class DummyContext:
        def __init__(self):
            self.settings = _ExecSettings(tmp_path)

    result = candidate_ranker_core.build_candidate_rankings(DummyContext(), slate_payload, market_matches)
    row = result["rows"][0]

    assert result["diagnostics"]["composite_markets"] == 1
    assert result["diagnostics"]["exact"] == 1
    assert result["diagnostics"]["composite_full_matches"] == 1
    assert result["diagnostics"]["composite_legs_exact"] == 2
    assert row["match_type"] == "exact"
    assert row["model_prob"] is not None
    assert row["sgp_adjusted_prob"] == row["model_prob"]
    assert row["consensus"]["raw_joint_prob"] > row["consensus"]["sgp_adjusted_prob"]


def test_candidate_ranker_blocks_composite_even_when_edge_and_liquidity_pass(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    candidate = {
        "candidate_id": "mlb:coloradorockies:losangelesdodgers",
        "sport": "mlb",
        "league": "MLB",
        "away_team": "Colorado Rockies",
        "home_team": "Los Angeles Dodgers",
        "start_time": "2026-07-06T22:10:00Z",
        "structured_sources": ["the_odds_api"],
        "line_markets": [
            {"provider": "the_odds_api", "book": book, "market": "h2h", "name": "Los Angeles Dodgers", "price": -1000}
            for book in ("pinnacle", "circa")
        ] + [
            {"provider": "the_odds_api", "book": book, "market": "h2h", "name": "Colorado Rockies", "price": 700}
            for book in ("pinnacle", "circa")
        ] + [
            {"provider": "the_odds_api", "book": book, "market": "totals", "name": "Over", "point": 9.5, "price": -1000}
            for book in ("pinnacle", "circa")
        ] + [
            {"provider": "the_odds_api", "book": book, "market": "totals", "name": "Under", "point": 9.5, "price": 700}
            for book in ("pinnacle", "circa")
        ],
    }
    slate_payload = {"generated_at": now, "candidates": [candidate]}
    market_matches = {
        "generated_at": now,
        "rows": [
            {
                "ticker": "KXMVE-EDGE-PASSES",
                "candidate_id": "kalshi:KXMVE-EDGE-PASSES",
                "sport": "mlb",
                "title": "yes Los Angeles D wins,yes Over 9.5 runs scored",
                "mve_selected_legs": [
                    {
                        "event_ticker": "KXMLBGAME-26JUL062210COLLAD",
                        "market_ticker": "KXMLBGAME-26JUL062210COLLAD-LAD",
                        "side": "yes",
                    },
                    {
                        "event_ticker": "KXMLBTOTAL-26JUL062210COLLAD",
                        "market_ticker": "KXMLBTOTAL-26JUL062210COLLAD-10",
                        "side": "yes",
                    },
                ],
                "kalshi_quote": {"bid": 9, "ask": 10, "mid": 10, "implied": 0.10, "spread_cents": 1},
                "orderbook": {
                    "yes": {
                        "best_bid_cents": 9,
                        "best_ask_cents": 10,
                        "spread_cents": 1,
                        "fillable_contracts": 20,
                        "vwap_cents": 10,
                        "captured_at": now,
                    }
                },
            }
        ],
    }

    class DummyContext:
        def __init__(self):
            self.settings = _ExecSettings(tmp_path)

    result = candidate_ranker_core.build_candidate_rankings(DummyContext(), slate_payload, market_matches)
    row = result["rows"][0]

    assert result["diagnostics"]["composite_markets"] == 1
    assert result["diagnostics"]["composite_trade_blocked"] == 1
    assert row["edge"] > row["required_edge"]
    assert row["passes_edge"] is False
    assert row["trade_eligible"] is False
    assert candidate_ranker_core.COMPOSITE_TRADE_BLOCKER in row["blockers"]


def test_sizing_defaults_to_quarter_kelly_and_reduces_correlated():
    base = sizing.capped_kelly(
        edge=0.10,
        market_prob=0.50,
        entry_price_cents=50,
        unit_cents=10000,
        max_units=100,
        min_edge=0.01,
    )
    validated = sizing.capped_kelly(
        edge=0.10,
        market_prob=0.50,
        entry_price_cents=50,
        unit_cents=10000,
        max_units=100,
        min_edge=0.01,
        validated=True,
    )
    correlated = sizing.capped_kelly(
        edge=0.10,
        market_prob=0.50,
        entry_price_cents=50,
        unit_cents=10000,
        max_units=100,
        min_edge=0.01,
        correlation_group_size=4,
    )

    assert base.adjusted_fraction == pytest.approx(0.05)
    assert validated.adjusted_fraction == pytest.approx(0.10)
    assert correlated.adjusted_fraction == pytest.approx(0.0125)


def test_status_reports_live_blockers(tmp_path, monkeypatch):
    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(status, "deliver", lambda *args, **kwargs: None)

    result = status.run(DummyContext(_ExecSettings(tmp_path)))

    assert result["live_ready"] is False
    assert "NBABOT_EXECUTION_MODE=live" in result["live_blockers"]
    assert (tmp_path / "TEST-GAME.status.json").exists()


def test_portfolio_sync_records_balance_and_positions(tmp_path, monkeypatch):
    class DummySettings(_ExecSettings):
        kalshi_game_tag = "TESTTAG"

    class DummyKalshi:
        def balance_cents(self):
            return 10000

        def positions(self):
            return {"KXTEST": 2}

    class DummyContext:
        settings = DummySettings(tmp_path)
        kalshi = DummyKalshi()
        game_tag = "TESTTAG"

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(portfolio_sync, "deliver", lambda *args, **kwargs: None)

    result = portfolio_sync.run(DummyContext())
    risk_rows = research.ResearchStore(DummyContext.settings.research_db_path).latest_rows(
        "risk_snapshots", 1,
    )

    assert result["ok"] is True
    assert result["balance_usd"] == 100.0
    assert result["positions"] == {"KXTEST": 2}
    assert risk_rows[0]["open_positions"] == 1


def test_source_registry_keeps_social_out_of_execution():
    config = source_registry.load_source_config()
    providers = config["providers"]

    assert "sportsgameodds" in providers
    assert providers["sportsgameodds"]["role"] == "primary_structured_odds"
    assert providers["parlay_api"]["role"] == "primary_structured_odds"
    assert providers["the_odds_api"]["role"] == "credit_gated_structured_odds_fallback"
    assert "execution" not in providers["parlay_api"]["allowed_for"]
    assert providers["parlay_api"]["live_execution_allowed"] is False
    assert providers["espn_public_site_api"]["role"] == "fallback_schedule_score_news"
    assert config["policy"]["social_may_trigger_execution"] is False
    assert config["policy"]["hidden_api_may_trigger_execution"] is False
    assert config["policy"]["execution_allowed_from"] == ["kalshi", "risk_gate"]
    assert providers["espn_public_site_api"]["live_execution_allowed"] is False
    assert "execution" not in providers["espn_public_site_api"]["allowed_for"]

    for key in config["policy"]["opinion_only_sources"]:
        assert providers[key]["live_execution_allowed"] is False
        assert "execution" not in providers[key]["allowed_for"]


def test_slate_discovery_handoff_defaults_to_kalshi_slate_limit(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    markets = [
        {
            "ticker": f"KXMLBGAME-TEST-{idx}",
            "event_ticker": "KXMLBGAME-TEST",
            "series_ticker": "KXMLBGAME",
            "title": f"MLB market {idx}",
            "bid": 40,
            "ask": 42,
            "implied": 0.41,
            "spread_cents": 2,
        }
        for idx in range(80)
    ]
    payload = {
        "game_id": "TEST-GAME",
        "generated_at": now,
        "candidate_count": 1,
        "sports": ["mlb"],
        "candidates": [
            {
                "candidate_id": "mlb:test",
                "sport": "mlb",
                "away_team": "Away",
                "home_team": "Home",
                "kalshi_markets": markets,
            }
        ],
    }

    class DummyContext:
        settings = _ExecSettings(tmp_path)

        def write_json(self, suffix, data):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(data, default=str))
            return path

    monkeypatch.setattr(slate_discovery, "discover_slate", lambda ctx: payload)
    monkeypatch.setattr(slate_discovery, "deliver", lambda *args, **kwargs: None)
    monkeypatch.delenv("NBABOT_SPORT_MARKET_CANDIDATES_LIMIT", raising=False)
    monkeypatch.delenv("NBABOT_SPORT_MARKET_CANDIDATES_MAX", raising=False)
    monkeypatch.setenv("NBABOT_KALSHI_SLATE_LIMIT", "75")

    slate_discovery.run(DummyContext())
    handoff = json.loads((tmp_path / "TEST-GAME.sport_market_candidates.json").read_text())

    assert len(handoff["rows"]) == 75
    assert handoff["rows"][0]["ticker"] == "KXMLBGAME-TEST-0"


def _odds_quota_source_config():
    return {
        "providers": {
            "the_odds_api": {
                "base_url": "https://api.the-odds-api.com/v4",
                "endpoints": {
                    "sports": "/sports/",
                    "odds": "/sports/{sport}/odds/",
                },
                "default_params": {
                    "odds": {
                        "regions": "us",
                        "markets": "h2h,spreads,totals",
                        "oddsFormat": "american",
                    }
                },
            }
        },
        "sports": {
            "mlb": {
                "odds_api_sport_key": "baseball_mlb",
            }
        },
    }


def test_the_odds_api_quota_guard_skips_paid_odds_below_floor(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "SECRET")
    monkeypatch.delenv("SPORTSGAMEODDS_API_KEY", raising=False)
    monkeypatch.delenv("PARLAY_API_KEY", raising=False)
    monkeypatch.delenv("NBABOT_DISABLE_THE_ODDS_API", raising=False)
    monkeypatch.delenv("NBABOT_THE_ODDS_API_ENABLED", raising=False)
    monkeypatch.setenv("NBABOT_THE_ODDS_API_MIN_REMAINING", "30")
    calls = []

    def fake_get_json(url, *, params=None, headers=None):
        calls.append(url)
        if url.endswith("/sports/"):
            return [], {"ok": True, "url": url, "x_requests_remaining": "20"}
        return [{"id": "paid"}], {"ok": True, "url": url, "x_requests_remaining": "19"}

    monkeypatch.setattr(slate, "_get_json", fake_get_json)

    events, statuses = slate.fetch_structured_events(["mlb"], _odds_quota_source_config())
    quota = next(row for row in statuses if row.get("provider") == "the_odds_api")

    assert events["the_odds_api"] == []
    assert quota["stage"] == "quota-preflight"
    assert quota["skipped"] is True
    assert quota["skip_reason"] == "credits-below-floor"
    assert quota["credits_remaining"] == 20
    assert not any(url.endswith("/odds/") for url in calls)


def test_the_odds_api_quota_guard_attempts_paid_odds_above_floor(monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "SECRET")
    monkeypatch.delenv("SPORTSGAMEODDS_API_KEY", raising=False)
    monkeypatch.delenv("PARLAY_API_KEY", raising=False)
    monkeypatch.delenv("NBABOT_DISABLE_THE_ODDS_API", raising=False)
    monkeypatch.delenv("NBABOT_THE_ODDS_API_ENABLED", raising=False)
    monkeypatch.setenv("NBABOT_THE_ODDS_API_MIN_REMAINING", "30")
    calls = []

    def fake_get_json(url, *, params=None, headers=None):
        calls.append(url)
        if url.endswith("/sports/"):
            return [], {"ok": True, "url": url, "x_requests_remaining": "31"}
        return [{"id": "paid", "bookmakers": []}], {"ok": True, "url": url, "x_requests_remaining": "30"}

    monkeypatch.setattr(slate, "_get_json", fake_get_json)

    events, statuses = slate.fetch_structured_events(["mlb"], _odds_quota_source_config())
    preflight = next(
        row for row in statuses
        if row.get("provider") == "the_odds_api" and row.get("stage") == "quota-preflight"
    )
    paid = next(
        row for row in statuses
        if row.get("provider") == "the_odds_api" and row.get("sport_key") == "baseball_mlb"
    )

    assert preflight["quota_ok"] is True
    assert paid["rows"] == 1
    assert events["the_odds_api"][0]["sport"] == "mlb"
    assert any(url.endswith("/sports/baseball_mlb/odds/") for url in calls)


def test_slate_defaults_to_broad_daily_sports(monkeypatch):
    monkeypatch.delenv("NBABOT_SLATE_SPORTS", raising=False)
    settings = _ExecSettings(Path("/tmp"))

    sports_list = slate.configured_sports(settings)

    assert "soccer_world_cup" in sports_list
    assert "mlb" in sports_list
    assert "nba_summer_league" in sports_list
    assert "nba" not in sports_list


def test_broad_kalshi_open_markets_become_research_watchlist(tmp_path, monkeypatch):
    class DummySettings(_ExecSettings):
        game = {"game": {}, "sources": {}}
        sport = "nba"
        kalshi_game_tag = "TESTTAG"

    class DummyKalshi:
        def __init__(self):
            self.series_calls = []

        def list_markets(self, series=None, status="open", max_pages=6):
            self.series_calls.append((series, status, max_pages))
            if series == "KXWCGAME":
                return [
                    {
                        "ticker": "KXWCGAME-26JUL07ESPPRT-ESP",
                        "event_ticker": "KXWCGAME-26JUL07ESPPRT",
                        "title": "Spain vs Portugal: Spain wins",
                        "yes_bid_dollars": "0.51",
                        "yes_ask_dollars": "0.53",
                        "close_time": "2026-07-07T20:00:00Z",
                        "status": "active",
                        "market_type": "binary",
                    }
                ]
            return []

        def list_open_markets(self, max_pages=3):
            return [
                {
                    "ticker": "KXSOCCER",
                    "event_ticker": "KXSOCCER-EVENT",
                    "title": "yes Spain advances,yes Reg Time: Over 2.5 goals scored",
                    "yes_bid_dollars": "0.20",
                    "yes_ask_dollars": "0.24",
                    "close_time": "2026-07-07T00:00:00Z",
                    "status": "active",
                    "market_type": "binary",
                }
            ]

    class DummyAdapter:
        def discovery_series(self, settings):
            return []

        def fetch_raw_markets(self, kalshi, game_tag, series):
            return []

        def build_catalog(self, game_id, game_tag, raw, scenarios):
            return []

    class DummyContext:
        settings = DummySettings(tmp_path)
        kalshi = DummyKalshi()
        adapter = DummyAdapter()
        game_tag = "TESTTAG"
        scenarios = []

        def read_json(self, suffix):
            return None

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setenv("NBABOT_SLATE_SPORTS", "soccer_world_cup")
    monkeypatch.setattr(
        slate,
        "fetch_structured_events",
        lambda sports, source_config: (
            {"sportsgameodds": [], "the_odds_api": [], "parlay_api": [], "espn_public_site_api": []},
            [],
        ),
    )
    ctx = DummyContext()
    payload = slate.discover_slate(ctx)
    tickers = [
        market["ticker"]
        for item in payload["candidates"]
        for market in item.get("kalshi_markets", [])
    ]
    candidate = next(
        item for item in payload["candidates"]
        if item["candidate_id"].startswith("kalshi:KXSOCCER")
    )

    assert ("KXWCGAME", "open", 20) in ctx.kalshi.series_calls
    assert "KXWCGAME-26JUL07ESPPRT-ESP" in tickers
    assert "KXSOCCER" in tickers
    assert candidate["kalshi_markets"][0]["bid"] == 20
    assert candidate["kalshi_markets"][0]["ask"] == 24

    research_payload = slate.research_bundle(
        DummyContext(),
        payload,
        {"rows": []},
    )

    research_row = next(
        item for item in research_payload["market_candidates"]
        if item["ticker"] == "KXSOCCER"
    )

    assert research_payload["candidate_count"] == 2
    assert research_row["trade_eligible"] is False
    assert "edge engine has not evaluated this market" in research_row["blockers"]


def test_demo_ranker_passed_open_market_flows_to_one_contract_intent(tmp_path):
    now = datetime.now(timezone.utc).isoformat()
    ticker = "KXMLBTOTAL-26JUL072005LAATEX-8"
    identity = {
        "sport": "mlb",
        "league": "MLB",
        "event_key": "mlb:2026-07-07:losangelesangels:texasrangers",
        "market_type": "total",
        "line": 7.5,
        "side": "over",
        "start_date": "2026-07-07",
        "participant": "over",
    }
    artifacts = {
        "market_snapshot.json": {"generated_at": now, "rows": []},
        "slate_candidates.json": {
            "generated_at": now,
            "candidates": [
                {
                    "candidate_id": f"kalshi:{ticker}",
                    "sport": "mlb",
                    "structured_sources": ["kalshi", "sportsgameodds"],
                    "kalshi_markets": [
                        {
                            "ticker": ticker,
                            "title": "Angels at Rangers total over 7.5",
                            "bid": 43,
                            "ask": 44,
                            "mid": 44,
                            "implied": 0.44,
                            "captured_at": now,
                        }
                    ],
                }
            ],
        },
        "slate_verification.json": {"verified_at": now, "rows": []},
        "book_watch.json": {
            "generated_at": now,
            "snapshots": [
                {
                    "captured_at": now,
                    "metrics": {
                        ticker: {
                            "yes": {
                                "captured_at": now,
                                "fillable_contracts": 1,
                                "spread_cents": 1,
                            }
                        }
                    },
                }
            ],
        },
        "market_matches.json": {
            "generated_at": now,
            "rows": [{"ticker": ticker, "captured_at": now, "freshness": {}}],
        },
        "candidate_ranker.json": {
            "generated_at": now,
            "rows": [
                {
                    "ticker": ticker,
                    "candidate_id": f"kalshi:{ticker}",
                    "model_prob": 0.48552,
                    "model_prob_sources": ["sportsgameodds"],
                    "edge": 0.04552,
                    "required_edge": 0.03745,
                    "passes_edge": True,
                    "book_count": 4,
                    "disagreement_std": 0.0066,
                    "market_identity": identity,
                    "is_composite": False,
                    "blockers": [],
                }
            ],
        },
    }

    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

        def read_json(self, suffix):
            return artifacts.get(suffix)

    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    settings.unit_usd = 5.0
    bundle = slate.research_bundle(
        DummyContext(settings),
        artifacts["slate_candidates.json"],
        artifacts["slate_verification.json"],
    )
    row = bundle["market_candidates"][0]

    assert bundle["trade_eligible_count"] == 1
    assert row["trade_eligible"] is True
    assert row["sgp_adjusted_prob"] == pytest.approx(row["model_prob"])
    assert row["blockers"] == []

    artifacts["research_bundle.json"] = bundle
    intent = paper._candidate_intents(DummyContext(settings))[0]
    decision = risk.evaluate_trade_intent(intent, settings)

    assert intent.ticker == ticker
    assert intent.contracts == 1
    assert intent.price_cents == 44
    assert intent.stake_units == pytest.approx(0.088)
    assert intent.sgp_adjusted_prob == pytest.approx(0.48552)
    assert intent.broad_slate is True
    assert decision.approved

    live_settings = _ExecSettings(tmp_path / "live")
    live_settings.execution_mode = "live"
    live_bundle = slate.research_bundle(
        DummyContext(live_settings),
        artifacts["slate_candidates.json"],
        artifacts["slate_verification.json"],
    )
    live_row = live_bundle["market_candidates"][0]
    assert live_bundle["trade_eligible_count"] == 0
    assert live_row["trade_eligible"] is False
    assert "not mapped to a configured scenario leg" in live_row["blockers"]


def test_source_check_reports_config_without_leaking_values(tmp_path, monkeypatch):
    monkeypatch.delenv("NBABOT_SOURCE_CHECK_NETWORK", raising=False)
    monkeypatch.setenv("SPORTSGAMEODDS_API_KEY", "SECRET-SGO")
    monkeypatch.setenv("THE_ODDS_API_KEY", "SECRET-ODDS")
    monkeypatch.setenv("PARLAY_API_KEY", "SECRET-PARLAY")

    report = source_registry.build_source_report(
        env={
            "SPORTSGAMEODDS_API_KEY": "SECRET-SGO",
            "THE_ODDS_API_KEY": "SECRET-ODDS",
            "PARLAY_API_KEY": "SECRET-PARLAY",
            "NBABOT_SOURCE_CHECK_NETWORK": "0",
        }
    )
    serialized = json.dumps(report)

    assert report["network_enabled"] is False
    assert "SECRET-SGO" not in serialized
    assert "SECRET-ODDS" not in serialized
    assert "SECRET-PARLAY" not in serialized
    assert next(p for p in report["providers"] if p["key"] == "sportsgameodds")["configured"]
    assert next(p for p in report["providers"] if p["key"] == "the_odds_api")["configured"]
    assert next(p for p in report["providers"] if p["key"] == "parlay_api")["configured"]

    class DummyContext:
        settings = _ExecSettings(tmp_path)

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(source_check, "deliver", lambda *args, **kwargs: None)
    result = source_check.run(DummyContext())

    assert result["network_enabled"] is False
    assert (tmp_path / "TEST-GAME.source_check.json").exists()


def test_slate_verify_rejects_espn_or_social_only_candidates(tmp_path):
    class DummyContext:
        settings = _ExecSettings(tmp_path)

    payload = {
        "candidates": [
            {
                "candidate_id": "nba:espn-only",
                "structured_sources": [],
                "fallback_sources": ["espn_public_site_api"],
                "kalshi_markets": [],
                "mapped_kalshi_markets": [],
            },
            {
                "candidate_id": "nba:social-only",
                "structured_sources": [],
                "fallback_sources": ["reddit"],
                "kalshi_markets": [],
                "mapped_kalshi_markets": [],
            },
        ],
    }

    result = slate.verify_slate(DummyContext(), payload)

    assert result["verified_count"] == 0
    assert all(not row["approved_for_research"] for row in result["rows"])
    assert all(not row["approved_for_execution"] for row in result["rows"])


def test_research_bundle_marks_trade_eligible_only_after_verified_slate_and_book(tmp_path):
    now = datetime.now(timezone.utc).isoformat()

    class DummyContext:
        settings = _ExecSettings(tmp_path)

        def read_json(self, suffix):
            if suffix == "market_snapshot.json":
                return {
                    "generated_at": now,
                    "rows": [
                        {
                            "ticker": "KXTEST",
                            "captured_at": now,
                            "scenario_id": "S1",
                            "market": "brunson_points",
                            "label": "Brunson points",
                            "side": "yes",
                            "entry_price_cents": 45,
                            "bid": 43,
                            "ask": 45,
                            "implied": 0.45,
                            "prior_p": 0.55,
                            "sgp_adjusted_prob": 0.22,
                            "risk": 3,
                        }
                    ]
                }
            if suffix == "book_watch.json":
                return {
                    "generated_at": now,
                    "snapshots": [
                        {
                            "captured_at": now,
                            "metrics": {
                                "KXTEST": {
                                    "yes": {
                                        "captured_at": now,
                                        "fillable_contracts": 10,
                                        "spread_cents": 2,
                                    }
                                }
                            }
                        }
                    ]
                }
            if suffix == "slate_candidates.json":
                return {"generated_at": now, "candidates": []}
            if suffix == "slate_verification.json":
                return {"verified_at": now, "rows": []}
            if suffix == "market_matches.json":
                return {
                    "generated_at": now,
                    "rows": [
                        {
                            "ticker": "KXTEST",
                            "orderbook_delta": {"signals": ["spread_tightening"]},
                            "external_matches": [{"candidate_id": "nba:verified", "score": 0.5}],
                            "freshness": {},
                        }
                    ],
                }
            return None

    slate_payload = {
        "candidates": [
            {
                "candidate_id": "nba:verified",
                "structured_sources": ["kalshi", "sportsgameodds"],
            }
        ]
    }
    verification = {
        "rows": [
            {
                "candidate_id": "nba:verified",
                "approved_for_research": True,
            }
        ]
    }

    bundle = slate.research_bundle(DummyContext(), slate_payload, verification)

    assert bundle["trade_eligible_count"] == 1
    candidate = bundle["market_candidates"][0]
    assert candidate["trade_eligible"] is True
    assert candidate["edge"] == 0.1
    assert "kalshi" in candidate["structured_sources"]


def test_research_bundle_flows_performance_validation_into_candidates(tmp_path):
    settings = _ExecSettings(tmp_path)
    store = research.ResearchStore(settings.research_db_path)
    for i in range(120):
        store.record_settlement(
            _settlement_fixture(
                i,
                ticker="KXMLBGAME-26JUL06NYYBOS-NYY",
                outcome=1 if i < 80 else 0,
                model_prob=0.70,
                market_prob=0.50,
                beat_closing=i < 75,
                clv_cents=6.0 if i < 75 else -2.0,
            )
        )

    now = datetime.now(timezone.utc).isoformat()
    ticker = "KXMLBGAME-26JUL06NYYBOS-NYY"
    slate_payload = {
        "generated_at": now,
        "candidates": [
            {
                "candidate_id": "mlb:verified",
                "structured_sources": ["kalshi", "the_odds_api"],
            }
        ],
    }
    verification = {
        "verified_at": now,
        "rows": [
            {
                "candidate_id": "mlb:verified",
                "approved_for_research": True,
            }
        ],
    }

    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

        def read_json(self, suffix):
            if suffix == "market_snapshot.json":
                return {
                    "generated_at": now,
                    "rows": [
                        {
                            "ticker": ticker,
                            "captured_at": now,
                            "scenario_id": "S1",
                            "market": "mlb_moneyline",
                            "label": "Yankees moneyline",
                            "side": "yes",
                            "entry_price_cents": 50,
                            "bid": 48,
                            "ask": 50,
                            "implied": 0.50,
                            "prior_p": 0.60,
                            "sgp_adjusted_prob": 0.60,
                            "risk": 3,
                        }
                    ],
                }
            if suffix == "book_watch.json":
                return {
                    "generated_at": now,
                    "snapshots": [
                        {
                            "captured_at": now,
                            "metrics": {
                                ticker: {
                                    "yes": {
                                        "captured_at": now,
                                        "fillable_contracts": 10,
                                        "spread_cents": 2,
                                    }
                                }
                            },
                        }
                    ],
                }
            if suffix == "slate_candidates.json":
                return slate_payload
            if suffix == "slate_verification.json":
                return verification
            if suffix == "market_matches.json":
                return {
                    "generated_at": now,
                    "rows": [
                        {
                            "ticker": ticker,
                            "orderbook_delta": {"signals": ["spread_tightening"]},
                            "external_matches": [{"candidate_id": "mlb:verified", "score": 0.9}],
                            "freshness": {},
                        }
                    ],
                }
            if suffix == "candidate_ranker.json":
                return {"generated_at": now, "rows": []}
            return None

    bundle = slate.research_bundle(DummyContext(settings), slate_payload, verification)
    candidate = bundle["market_candidates"][0]

    assert candidate["market_family"] == "mlb moneyline"
    assert candidate["validated"] is True
    assert candidate["market_type_verdict"] == "validated"
    assert candidate["performance"]["settled_count"] == 120
    assert candidate["performance"]["brier_comparison_count"] == 120
    assert bundle["performance_learning"]["families"]["mlb moneyline"]["validated"] is True


def test_paper_execution_uses_research_bundle_filter(tmp_path):
    class DummyContext:
        settings = _ExecSettings(tmp_path)
        empty_bundle = False

        def read_json(self, suffix):
            if suffix == "market_snapshot.json":
                return {
                    "rows": [
                        {
                            "game_id": "TEST-GAME",
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "scenario_id": "S1",
                            "market": "brunson_points",
                            "label": "Brunson points",
                            "ticker": "KXTEST",
                            "bid": 40,
                            "ask": 45,
                            "implied": 0.45,
                            "prior_p": 0.60,
                            "risk": 3,
                            "side": "yes",
                            "entry_price_cents": 45,
                            "sgp_adjusted_prob": 0.22,
                        }
                    ]
                }
            if suffix == "research_bundle.json":
                if self.empty_bundle:
                    return {"market_candidates": []}
                return {
                    "market_candidates": [
                        {
                            "ticker": "KXTEST",
                            "scenario_id": "S1",
                            "market": "brunson_points",
                            "trade_eligible": False,
                        }
                    ]
                }
            return None

    ctx = DummyContext()
    assert paper._candidate_intents(ctx) == []
    ctx.empty_bundle = True
    assert paper._candidate_intents(ctx) == []


def test_daily_cycle_skips_paper_execution_by_default(tmp_path, monkeypatch):
    calls = []

    class DummyContext:
        def __init__(self, settings):
            self.settings = settings

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    def step(name):
        def run(ctx):
            calls.append(name)
            return {"step": name}
        return run

    monkeypatch.setattr(portfolio_sync, "run", step("portfolio-sync"))
    monkeypatch.setattr(source_check, "run", step("source-check"))
    monkeypatch.setattr(news_ingest, "run", step("news-ingest"))
    monkeypatch.setattr(slate_discovery, "run", step("slate-discovery"))
    monkeypatch.setattr(slate_verify, "run", step("slate-verify"))
    monkeypatch.setattr(discover_markets, "run", step("discover-markets"))
    monkeypatch.setattr(snapshot_market, "run", step("snapshot-market"))
    monkeypatch.setattr(book_watch, "run", step("book-watch"))
    monkeypatch.setattr(market_matcher, "run", step("market-matcher"))
    monkeypatch.setattr(qual_research_agent, "run", step("qual-research"))
    monkeypatch.setattr(candidate_ranker, "run", step("candidate-ranker"))
    monkeypatch.setattr(research_agent, "run", step("research-agent"))
    monkeypatch.setattr(status, "run", step("status"))
    monkeypatch.setattr(daily_cycle, "deliver", lambda *args, **kwargs: None)

    result = daily_cycle.run(DummyContext(_ExecSettings(tmp_path)))

    assert calls == [
        "source-check",
        "news-ingest",
        "slate-discovery",
        "slate-verify",
        "portfolio-sync",
        "discover-markets",
        "snapshot-market",
        "book-watch",
        "market-matcher",
        "qual-research",
        "candidate-ranker",
        "research-agent",
        "status",
    ]
    assert any(
        step["name"] == "execution" and step["status"] == "skipped"
        for step in result["steps"]
    )
    assert result["steps"][0]["activates"] == ["news-ingest"]
    assert result["steps"][1]["activated_by"] == "source-check"
    assert result["steps"][2]["activated_by"] == "news-ingest"
    assert {"from": "book-watch", "to": "market-matcher", "status": "ok"} in result["activation_edges"]
    assert {"from": "market-matcher", "to": "qual-research", "status": "ok"} in result["activation_edges"]
    assert {"from": "qual-research", "to": "candidate-ranker", "status": "ok"} in result["activation_edges"]
    assert {"from": "candidate-ranker", "to": "research-agent", "status": "ok"} in result["activation_edges"]
    assert {"from": "research-agent", "to": "execution", "status": "skipped"} in result["activation_edges"]
    assert (tmp_path / "TEST-GAME.daily_cycle.json").exists()


def test_scheduled_demo_cycle_reports_once_after_settlement_status(tmp_path, monkeypatch):
    calls = []
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "paper"
    settings.deliver_to = "telegram:123"

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    def fake_daily(ctx):
        assert ctx.settings.execution_mode == "demo"
        assert ctx.settings.deliver_to == "stdout"
        calls.append("daily-cycle")
        return {
            "steps": [
                {
                    "name": "slate-discovery",
                    "status": "ok",
                    "result": {"sports": ["mlb", "nba"], "candidate_count": 3},
                },
                {
                    "name": "candidate-ranker",
                    "status": "ok",
                    "result": {
                        "candidate_count": 10,
                        "edge_pass_count": 2,
                        "trade_eligible_count": 1,
                    },
                },
                {
                    "name": "research-agent",
                    "status": "ok",
                    "result": {"candidate_count": 10, "trade_eligible_count": 1},
                },
                {
                    "name": "demo-execute",
                    "status": "ok",
                    "result": {
                        "intent": {"ticker": "KXDEMO-YES"},
                        "receipt": {"status": "submitted", "response": {}},
                    },
                },
            ],
        }

    def fake_settlement(ctx):
        assert ctx.settings.deliver_to == "stdout"
        calls.append("settlement-audit")
        return {
            "checked_count": 1,
            "pending_count": 0,
            "error_count": 0,
            "summary": {"settled_count": 1},
        }

    def fake_status(ctx):
        assert ctx.settings.deliver_to == "stdout"
        calls.append("status")
        return {"execution_mode": ctx.settings.execution_mode}

    def fake_postmortem(ctx):
        assert ctx.settings.deliver_to == "stdout"
        calls.append("qual-postmortem")
        return {"completed_count": 1, "queued_count": 0, "missing_recap_count": 0}

    messages = []
    monkeypatch.setattr(daily_cycle, "run", fake_daily)
    monkeypatch.setattr(settlement_audit, "run", fake_settlement)
    monkeypatch.setattr(qual_postmortem, "run", fake_postmortem)
    monkeypatch.setattr(status, "run", fake_status)
    monkeypatch.setattr(
        scheduled_demo_cycle,
        "deliver",
        lambda text, to="stdout": messages.append((text, to)) or True,
    )

    result = scheduled_demo_cycle.run(DummyContext())

    assert calls == ["daily-cycle", "settlement-audit", "qual-postmortem", "status"]
    assert settings.execution_mode == "demo"
    assert settings.deliver_to == "telegram:123"
    assert result["exit_code"] == 0
    assert result["demo_orders_placed"] == 1
    assert result["demo_order_tickers"] == ["KXDEMO-YES"]
    assert result["report_delivery_ok"] is True
    assert len(messages) == 1
    assert messages[0][1] == "telegram:123"
    assert "mode=demo" in messages[0][0]
    assert "sports=mlb,nba" in messages[0][0]
    assert "candidates=10 edges_found=2 trade_eligible=1" in messages[0][0]
    assert "demo_orders=1 tickers=KXDEMO-YES" in messages[0][0]
    assert "settlement checked=1 settled=1 pending=0 errors=0" in messages[0][0]
    assert "postmortems completed=1 queued=0 missing_recaps=0" in messages[0][0]
    assert (tmp_path / "TEST-GAME.scheduled_demo_cycle.json").exists()


def test_scheduled_demo_cycle_soft_reports_missing_demo_credentials(tmp_path, monkeypatch):
    settings = _ExecSettings(tmp_path)
    settings.execution_mode = "demo"
    settings.deliver_to = "telegram"

    class DummyContext:
        def __init__(self):
            self.settings = settings

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    def fake_daily(ctx):
        return {
            "steps": [
                {
                    "name": "candidate-ranker",
                    "status": "ok",
                    "result": {"candidate_count": 200, "edge_pass_count": 0},
                },
                {
                    "name": "research-agent",
                    "status": "ok",
                    "result": {"candidate_count": 200, "trade_eligible_count": 0},
                },
                {
                    "name": "demo-execute",
                    "status": "ok",
                    "result": {
                        "reason": "mode-blocked",
                        "detail": DEMO_CREDENTIALS_BLOCKED_REASON,
                    },
                },
            ],
        }

    messages = []
    monkeypatch.setattr(daily_cycle, "run", fake_daily)
    monkeypatch.setattr(settlement_audit, "run", lambda ctx: {"summary": {}})
    monkeypatch.setattr(qual_postmortem, "run", lambda ctx: {})
    monkeypatch.setattr(status, "run", lambda ctx: {})
    monkeypatch.setattr(
        scheduled_demo_cycle,
        "deliver",
        lambda text, to="stdout": messages.append((text, to)) or False,
    )

    result = scheduled_demo_cycle.run(DummyContext())

    assert result["exit_code"] == 0
    assert result["report_delivery_ok"] is False
    assert result["blocked_reason"] == f"ran, could not trade: {DEMO_CREDENTIALS_BLOCKED_REASON}"
    assert result["hard_error"] is None
    assert "ran, could not trade: set KALSHI_DEMO_API_KEY" in messages[0][0]


def test_cli_returns_phase_exit_code(monkeypatch):
    monkeypatch.setitem(cli.PHASES, "fake-exit", lambda ctx: {"exit_code": 7})
    monkeypatch.setattr(cli, "load_context", lambda game_id=None: object())

    assert cli.main(["fake-exit"]) == 7


def test_demo_scheduler_files_force_demo_without_live_gates():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scheduler" / "run-demo-cycle.sh").read_text()
    crontab = (root / "scheduler" / "demo-crontab.txt").read_text()
    combined = script + "\n" + crontab

    assert "NBABOT_EXECUTION_MODE=demo" in script
    assert "scheduled-demo-cycle" in script
    assert "NBABOT_EXECUTION_MODE=live" not in combined
    assert "NBABOT_LIVE_TRADING_ACK" not in combined
    assert "LIVE_TRADES_REAL_MONEY" not in combined
    assert "CRON_TZ=America/New_York" in crontab
    assert "crontab scheduler/demo-crontab.txt" in crontab
    assert "caffeinate -s" in crontab
    assert crontab.count("$ENTRYPOINT") == 4
    assert "date +\\%Y\\%m\\%dT\\%H\\%M\\%S" in crontab


def test_sports_port_registry_lists_core_ports():
    registry = {port["key"]: port for port in sports.list_ports()}

    assert {"nba", "soccer", "mlb", "nfl", "nhl", "cbb", "tennis"} <= registry.keys()
    assert registry["nba"]["status"] == "active-runtime"
    assert registry["soccer"]["status"] == "research-only"
    assert "orderbook.py" not in registry["nba"]["modules"]
    assert any("starting pitcher" in blocker for blocker in registry["mlb"]["blockers"])


def test_ports_phase_writes_registry(tmp_path, monkeypatch):
    class DummySettings:
        game_id = "TEST-GAME"
        deliver_to = "stdout"

        def data_path(self, suffix):
            return tmp_path / f"{self.game_id}.{suffix}"

    class DummyContext:
        settings = DummySettings()

        def write_json(self, suffix, payload):
            path = self.settings.data_path(suffix)
            path.write_text(json.dumps(payload, default=str))
            return path

    monkeypatch.setattr(ports, "deliver", lambda *args, **kwargs: None)

    result = ports.run(DummyContext())
    written = json.loads((tmp_path / "TEST-GAME.sports_ports.json").read_text())

    assert result["preferred_cli"] == "ksobot"
    assert written["compatibility_package"] == "nbabot"
    assert any(port["key"] == "nba" for port in written["ports"])


def test_new_automation_phases_registered():
    assert "discover-markets" in PHASES
    assert "autopilot" in PHASES
    assert "daily-cycle" in PHASES
    assert "live-execute" in PHASES
    assert "book-watch" in PHASES
    assert "candidate-ranker" in PHASES
    assert "market-matcher" in PHASES
    assert "portfolio-sync" in PHASES
    assert "source-check" in PHASES
    assert "slate-discovery" in PHASES
    assert "slate-verify" in PHASES
    assert "research-agent" in PHASES
    assert "qual-postmortem" in PHASES
    assert "settlement-audit" in PHASES
    assert "ports" in PHASES
    assert "status" in PHASES
    assert "telegram-test" in PHASES
    assert "scheduled-demo-cycle" in PHASES


def test_june_24_world_cup_slate_defers_line_analysis():
    import yaml

    root = Path(__file__).resolve().parents[1]
    slate = yaml.safe_load((root / "config" / "WC-2026-JUN24.slate.yaml").read_text())

    assert slate["slate"]["line_analysis_status"] == "deferred"
    assert slate["slate"]["market_alignment_status"] == "complete"
    assert slate["slate"]["execution_status"] == "disabled"
    report = root / slate["slate"]["market_alignment_report"]
    assert report.exists()
    assert "No parlay is constructed" in report.read_text()
    assert len(slate["matches"]) == 6
    assert len(slate["scenarios"]) == 12

    forbidden = {
        "ticker", "bid_cents", "ask_cents", "market_implied_p", "edge",
        "stake_units", "suggested_stake_units", "payout_x",
    }
    for scenario in slate["scenarios"]:
        assert scenario["pricing_status"] == "deferred"
        assert forbidden.isdisjoint(scenario)


def test_june_25_world_cup_slate_is_research_only():
    import yaml

    root = Path(__file__).resolve().parents[1]
    slate = yaml.safe_load((root / "config" / "WC-2026-JUN25.slate.yaml").read_text())

    assert slate["slate"]["date"] == "2026-06-25"
    assert slate["slate"]["scoreline_order"] == "listed_match_order"
    assert slate["slate"]["line_analysis_status"] == "deferred"
    assert slate["slate"]["market_alignment_status"] == "complete"
    assert slate["slate"]["execution_status"] == "disabled"
    report = root / slate["slate"]["market_alignment_report"]
    report_text = report.read_text()
    assert "No parlay is constructed" in report_text
    assert "No edge conclusion" in report_text
    assert len(slate["matches"]) == 6
    assert len(slate["scenarios"]) == 12

    forbidden = {
        "ticker", "bid_cents", "ask_cents", "market_implied_p", "edge",
        "stake_units", "suggested_stake_units", "payout_x",
    }
    matches = {match["id"]: match for match in slate["matches"]}
    for scenario in slate["scenarios"]:
        assert scenario["pricing_status"] == "deferred"
        assert scenario["probability_role"] == "market_alignment_only"
        assert 0 < scenario["uncertainty_adjusted_probability"] < 1
        assert forbidden.isdisjoint(scenario)

        expected_goals = list(
            matches[scenario["match_id"]]["market_expected_goals"].values()
        )
        accepted_scores = set(scenario["scoreline_set"])
        probability = soccer_research.scoreline_probability(
            expected_goals[0],
            expected_goals[1],
            lambda home, away: f"{home}-{away}" in accepted_scores,
        )
        assert scenario["scoreline_set_probability"] == pytest.approx(
            probability, abs=0.0001
        )
        assert scenario["uncertainty_adjusted_probability"] == pytest.approx(
            soccer_research.uncertainty_adjust(probability), abs=0.0001
        )


def test_june_25_potential_plays_are_bounded_watchlist():
    import yaml

    root = Path(__file__).resolve().parents[1]
    slate = yaml.safe_load((root / "config" / "WC-2026-JUN25.slate.yaml").read_text())
    plays_path = root / slate["slate"]["potential_plays_file"]
    report_path = root / slate["slate"]["potential_plays_report"]
    document = yaml.safe_load(plays_path.read_text())
    watchlist = document["watchlist"]
    matches = document["matches"]

    assert slate["slate"]["potential_plays_status"] == "complete"
    assert watchlist["execution_status"] == "disabled"
    assert watchlist["edge_status"] == "not_estimated"
    assert watchlist["stake_status"] == "none"
    assert watchlist["parlay_status"] == "none"
    assert report_path.exists()
    assert guardrails.GUARDRAIL_FOOTER in report_path.read_text()
    assert len(matches) == 6

    forbidden = {
        "edge", "stake_units", "suggested_stake_units", "contracts",
        "client_order_id", "joint_probability", "sgp_payout_x",
    }
    for match in matches:
        assert 1 <= len(match["plays"]) <= 2
        for play in match["plays"]:
            assert forbidden.isdisjoint(play)
            assert play["sgp_adjustment"] == "not_applicable_single_market"
            assert play["side"] in {"yes", "no"}
            assert 0 < play["ask_cents"] < 100
            assert play["market_implied_probability"] == pytest.approx(
                play["ask_cents"] / 100
            )
            assert play["gross_payout_x"] == pytest.approx(
                100 / play["ask_cents"], abs=0.01
            )


def test_june_25_edge_analysis_uses_independent_prices_and_fees():
    import yaml

    root = Path(__file__).resolve().parents[1]
    slate = yaml.safe_load((root / "config" / "WC-2026-JUN25.slate.yaml").read_text())
    edge_path = root / slate["slate"]["edge_analysis_file"]
    report_path = root / slate["slate"]["edge_analysis_report"]
    document = yaml.safe_load(edge_path.read_text())
    analysis = document["edge_analysis"]
    candidates = [
        candidate
        for match in document["matches"]
        for candidate in match["candidates"]
    ]

    assert slate["slate"]["edge_analysis_status"] == "complete"
    assert analysis["execution_status"] == "disabled"
    assert analysis["normal_minimum_edge"] == 0.05
    assert analysis["actionable_candidates"] == 0
    assert analysis["research_override_supported"] is False
    assert len(candidates) == 12
    assert "No candidate clears" in report_path.read_text()
    assert guardrails.GUARDRAIL_FOOTER in report_path.read_text()

    verified = [candidate for candidate in candidates
                if candidate["fair_probability"] is not None]
    assert verified
    assert max(candidate["gross_edge"] for candidate in verified) < 0.05

    for candidate in verified:
        price = candidate["kalshi_ask_probability"]
        fair = candidate["fair_probability"]
        assert candidate["gross_edge"] == pytest.approx(fair - price, abs=0.0001)
        fee_load = 0.07 * price * (1 - price)
        assert candidate["estimated_taker_fee_load"] == pytest.approx(
            fee_load, abs=0.0001
        )
        assert candidate["fee_adjusted_edge"] == pytest.approx(
            candidate["gross_edge"] - fee_load, abs=0.0001
        )
        assert candidate["max_ask_to_clear_normal_gate_cents"] <= int(
            100 * (fair - analysis["normal_minimum_edge"])
        )

    usa_no = next(
        candidate for candidate in candidates
        if candidate["label"] == "USA does not win"
    )
    assert usa_no["classification"] == "watch_below_gate"
    assert usa_no["gross_edge"] > 0
    assert usa_no["fee_adjusted_edge"] > 0
