"""Paper/demo/live execution framework. Live trading is explicitly gated."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .audit import AuditTrail
from .research import ResearchStore, utc_now
from .risk import RiskDecision


@dataclass(frozen=True)
class TradeIntent:
    game_id: str
    scenario_id: str
    ticker: str | None
    action: str
    side: str
    contracts: int
    price_cents: int
    stake_units: float
    model_prob: float
    market_prob: float | None
    edge: float | None
    sgp_adjusted_prob: float | None
    risk: int
    hope_bet: bool
    captured_at: str
    bid_cents: int | None = None
    ask_cents: int | None = None
    rationale: str = ""


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    ticker: str
    action: str
    side: str
    order_type: str
    count: int
    price_cents: int

    def kalshi_v2_body(self) -> dict[str, Any]:
        order_side = "bid" if self.side == "yes" else "ask"
        price_cents = self.price_cents if self.side == "yes" else 100 - self.price_cents
        return {
            "client_order_id": self.client_order_id,
            "ticker": self.ticker,
            "side": order_side,
            "count": f"{self.count:.2f}",
            "price": f"{price_cents / 100:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }


@dataclass(frozen=True)
class OrderReceipt:
    client_order_id: str
    mode: str
    status: str
    response: dict[str, Any]


@dataclass(frozen=True)
class PaperFill:
    client_order_id: str
    ticker: str
    side: str
    contracts: int
    price_cents: int
    filled_at: str


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def client_order_id(intent: TradeIntent, mode: str) -> str:
    raw = json.dumps({
        "mode": mode,
        "game_id": intent.game_id,
        "scenario_id": intent.scenario_id,
        "ticker": intent.ticker,
        "action": intent.action,
        "side": intent.side,
        "contracts": intent.contracts,
        "price_cents": intent.price_cents,
    }, sort_keys=True)
    return "nbabot-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_order_request(intent: TradeIntent, mode: str) -> OrderRequest:
    if not intent.ticker:
        raise ValueError("cannot build order request without a ticker")
    return OrderRequest(
        client_order_id=client_order_id(intent, mode),
        ticker=intent.ticker,
        action=intent.action,
        side=intent.side,
        order_type="limit",
        count=int(intent.contracts),
        price_cents=int(intent.price_cents),
    )


def execute_paper(intent: TradeIntent, decision: RiskDecision, settings: Any,
                  store: ResearchStore, audit: AuditTrail) -> OrderReceipt:
    request = build_order_request(intent, "paper")
    if not decision.approved:
        receipt = OrderReceipt(request.client_order_id, "paper", "rejected",
                               {"reasons": decision.reasons})
        audit.log("PAPER_REJECTED", asdict(receipt), intent.game_id)
        return receipt

    fill = PaperFill(
        client_order_id=request.client_order_id,
        ticker=request.ticker,
        side=request.side,
        contracts=request.count,
        price_cents=request.price_cents,
        filled_at=utc_now(),
    )
    receipt = OrderReceipt(request.client_order_id, "paper", "filled", asdict(fill))
    inserted = store.record_order("paper_orders", intent.game_id, intent, decision, request, receipt)
    if inserted:
        store.record_fill(intent.game_id, request.client_order_id, fill)
        _append_jsonl(settings.data_path("paper_orders.jsonl"), {
            "intent": asdict(intent),
            "decision": asdict(decision),
            "request": asdict(request),
            "receipt": asdict(receipt),
        })
    audit.log("PAPER_ORDER", {"inserted": inserted, **asdict(receipt)}, intent.game_id)
    return receipt


def execute_demo(intent: TradeIntent, decision: RiskDecision, settings: Any,
                 store: ResearchStore, audit: AuditTrail, kalshi: Any) -> OrderReceipt:
    if settings.execution_mode != "demo":
        raise RuntimeError("demo execution requires NBABOT_EXECUTION_MODE=demo")
    request = build_order_request(intent, "demo")
    if not decision.approved:
        receipt = OrderReceipt(request.client_order_id, "demo", "rejected",
                               {"reasons": decision.reasons})
        audit.log("DEMO_REJECTED", asdict(receipt), intent.game_id)
        return receipt

    body = request.kalshi_v2_body()
    try:
        response = kalshi.demo_place_order(settings.demo_api_base, body)
    except Exception as e:
        audit.dead_letter("DEMO_ORDER", str(e), {"body": body}, intent.game_id)
        raise

    receipt = OrderReceipt(request.client_order_id, "demo", "submitted", response)
    inserted = store.record_order("demo_orders", intent.game_id, intent, decision, request, receipt)
    if inserted:
        _append_jsonl(settings.data_path("demo_orders.jsonl"), {
            "intent": asdict(intent),
            "decision": asdict(decision),
            "request": asdict(request),
            "receipt": asdict(receipt),
        })
    audit.log("DEMO_ORDER", {"inserted": inserted, **asdict(receipt)}, intent.game_id)
    return receipt


def execute_live(intent: TradeIntent, decision: RiskDecision, settings: Any,
                 store: ResearchStore, audit: AuditTrail, kalshi: Any) -> OrderReceipt:
    if settings.execution_mode != "live":
        raise RuntimeError("live execution requires NBABOT_EXECUTION_MODE=live")
    if getattr(settings, "dry_run", True):
        raise RuntimeError("live execution requires NBABOT_DRY_RUN=0")
    if getattr(settings, "live_trading_ack", "") != "LIVE_TRADES_REAL_MONEY":
        raise RuntimeError(
            "live execution requires NBABOT_LIVE_TRADING_ACK=LIVE_TRADES_REAL_MONEY"
        )

    request = build_order_request(intent, "live")
    if store.order_exists("live_orders", request.client_order_id):
        receipt = OrderReceipt(request.client_order_id, "live", "duplicate",
                               {"reason": "client_order_id already recorded"})
        audit.log("LIVE_DUPLICATE", asdict(receipt), intent.game_id)
        return receipt
    if not decision.approved:
        receipt = OrderReceipt(request.client_order_id, "live", "rejected",
                               {"reasons": decision.reasons})
        audit.log("LIVE_REJECTED", asdict(receipt), intent.game_id)
        return receipt

    body = request.kalshi_v2_body()
    try:
        response = kalshi.place_order(body)
    except Exception as e:
        audit.dead_letter("LIVE_ORDER", str(e), {"body": body}, intent.game_id)
        raise

    receipt = OrderReceipt(request.client_order_id, "live", "submitted", response)
    inserted = store.record_order("live_orders", intent.game_id, intent, decision, request, receipt)
    if inserted:
        _append_jsonl(settings.data_path("live_orders.jsonl"), {
            "intent": asdict(intent),
            "decision": asdict(decision),
            "request": asdict(request),
            "receipt": asdict(receipt),
        })
    audit.log("LIVE_ORDER", {"inserted": inserted, **asdict(receipt)}, intent.game_id)
    return receipt
