"""Resolve filled execution ledger rows against Kalshi settlement truth."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import calibration
from .research import utc_now


SETTLEMENT_EVENT_TYPE = "SETTLEMENT_AUDIT_RECORD"
TERMINAL_STATUSES = {"settled", "finalized", "resolved", "determined"}


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _num(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _prob(raw: Any) -> float | None:
    value = _num(raw)
    if value is None:
        return None
    if value > 1:
        value /= 100.0
    if value < 0 or value > 1:
        return None
    return value


def _price_cents(raw: Any) -> float | None:
    value = _num(raw)
    if value is None:
        return None
    if 0 <= value <= 1:
        value *= 100
    if value < 0 or value > 100:
        return None
    return round(value, 4)


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _side(raw: Any) -> str | None:
    text = str(raw or "").strip().lower()
    if text in {"yes", "y", "true", "1"}:
        return "yes"
    if text in {"no", "n", "false", "0"}:
        return "no"
    return None


def _settlement_value_side(raw: Any) -> str | None:
    value = _num(raw)
    if value is None:
        return None
    if 0 <= value <= 1:
        return "yes" if value >= 0.5 else "no"
    if 0 <= value <= 100:
        return "yes" if value >= 50 else "no"
    return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _winning_side(market: dict[str, Any], terminal: bool = False) -> str | None:
    for key in (
        "winning_side", "winner", "result", "outcome", "settlement_result",
        "market_result",
    ):
        side = _side(market.get(key))
        if side:
            return side
    if not terminal:
        return None
    for key in ("yes_won", "yes_wins", "yes_win"):
        if isinstance(market.get(key), bool):
            return "yes" if market[key] else "no"
    for key in (
        "settlement_value", "settled_value", "payout_value", "final_value",
        "expiration_value",
    ):
        side = _settlement_value_side(market.get(key))
        if side:
            return side
    return None


def market_resolution(market: dict[str, Any]) -> dict[str, Any]:
    """Return normalized settlement status from a Kalshi market payload."""
    status = str(
        market.get("status")
        or market.get("settlement_status")
        or market.get("market_status")
        or ""
    ).lower()
    winner = _winning_side(market, terminal=status in TERMINAL_STATUSES)
    settled_at = (
        market.get("settled_at")
        or market.get("settlement_time")
        or market.get("settled_time")
        or market.get("close_time")
    )
    return {
        "settled": bool(winner),
        "market_status": status or None,
        "winning_side": winner,
        "settled_at": settled_at,
    }


def _fill_contracts(fill: dict[str, Any]) -> float | None:
    return _num(_first_present(
        fill.get("contracts"),
        fill.get("count"),
        fill.get("fill_count"),
        fill.get("filled_count"),
        fill.get("count_fp"),
        fill.get("quantity"),
    ))


def _fill_price(fill: dict[str, Any]) -> float | None:
    side = str(fill.get("side") or fill.get("outcome_side") or "yes").lower()
    side_price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
    for key in (
        "price_cents", "average_fill_price", "avg_price", "price",
        side_price_key, "yes_price_dollars", "no_price_dollars",
    ):
        price = _price_cents(fill.get(key))
        if price is not None:
            return price
    return None


def _aggregate_fill(fill_docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    weighted = 0.0
    contracts = 0.0
    fees = 0.0
    has_fee = False
    for fill in fill_docs:
        count = _fill_contracts(fill)
        price = _fill_price(fill)
        if count is None or count <= 0 or price is None:
            continue
        contracts += count
        weighted += count * price
        fee = _num(fill.get("fee_cents"))
        if fee is not None:
            fees += fee
            has_fee = True
    if contracts <= 0:
        return None
    first = fill_docs[0] if fill_docs else {}
    return {
        "contracts": contracts,
        "entry_price_cents": round(weighted / contracts, 4),
        "filled_at": first.get("filled_at"),
        "fee_cents": round(fees, 4) if has_fee else None,
        "source": "fills",
    }


def execution_fill(order: dict[str, Any]) -> dict[str, Any] | None:
    """Build one exposure row from fills or a filled receipt."""
    request = _loads(order.get("request_json"), {}) or {}
    receipt = _loads(order.get("receipt_json"), {}) or {}
    fill_docs = [
        _loads(fill.get("fill_json"), {}) or {}
        for fill in order.get("fills") or []
    ]
    fill = _aggregate_fill(fill_docs)
    if fill:
        return fill

    response = receipt.get("response") if isinstance(receipt, dict) else {}
    response = response if isinstance(response, dict) else {}
    response_fill = _aggregate_fill([response])
    if response_fill:
        response_fill["source"] = "receipt"
        return response_fill

    status = str(receipt.get("status") or "").lower() if isinstance(receipt, dict) else ""
    if status == "filled":
        count = _num(request.get("count"))
        price = _price_cents(request.get("price_cents"))
        if count is not None and count > 0 and price is not None:
            return {
                "contracts": count,
                "entry_price_cents": price,
                "filled_at": response.get("filled_at"),
                "source": "filled_receipt_request",
            }
    return None


def normalized_order(order: dict[str, Any]) -> dict[str, Any] | None:
    intent = _loads(order.get("intent_json"), {}) or {}
    request = _loads(order.get("request_json"), {}) or {}
    fill = execution_fill(order)
    if fill is None:
        return None

    ticker = request.get("ticker") or intent.get("ticker")
    side = _side(request.get("side") or intent.get("side"))
    if not ticker or not side:
        return None

    return {
        "client_order_id": order["client_order_id"],
        "game_id": order["game_id"],
        "mode": order["mode"],
        "ticker": str(ticker),
        "side": side,
        "action": request.get("action") or intent.get("action") or "buy",
        "contracts": fill["contracts"],
        "entry_price_cents": fill["entry_price_cents"],
        "filled_at": fill.get("filled_at"),
        "fill_source": fill.get("source"),
        "fee_cents": fill.get("fee_cents"),
        "scenario_id": intent.get("scenario_id"),
        "entry_model_prob": _prob(intent.get("model_prob")),
        "entry_market_prob": _prob(intent.get("market_prob")),
        "signal_source": str(intent.get("signal_source") or order.get("signal_source") or "consensus"),
        "confluence_verdict": str(
            intent.get("confluence_verdict")
            or order.get("confluence_verdict")
            or "none"
        ),
        "confluence": intent.get("confluence") if isinstance(intent.get("confluence"), dict) else {},
        "intent": intent,
        "request": request,
    }


def normalized_shadow_intent(row: dict[str, Any]) -> dict[str, Any] | None:
    intent = _loads(row.get("intent_json"), {}) or {}
    ticker = row.get("ticker") or intent.get("ticker")
    side = _side(row.get("side") or intent.get("side"))
    if not ticker or not side:
        return None
    return {
        "client_order_id": row["client_order_id"],
        "game_id": row["game_id"],
        "mode": row.get("mode") or "shadow",
        "ticker": str(ticker),
        "side": side,
        "action": intent.get("action") or "buy",
        "contracts": _num(row.get("contracts")) or _num(intent.get("contracts")) or 0,
        "entry_price_cents": _price_cents(row.get("price_cents") or intent.get("price_cents")) or 0,
        "filled_at": None,
        "fill_source": "shadow_intent",
        "scenario_id": intent.get("scenario_id"),
        "entry_model_prob": _prob(intent.get("model_prob")),
        "entry_market_prob": _prob(intent.get("market_prob")),
        "signal_source": str(intent.get("signal_source") or row.get("signal_source") or "consensus"),
        "confluence_verdict": str(
            intent.get("confluence_verdict")
            or row.get("confluence_verdict")
            or "none"
        ),
        "confluence": intent.get("confluence") if isinstance(intent.get("confluence"), dict) else {},
        "intent": intent,
        "request": {
            "ticker": ticker,
            "side": side,
            "action": intent.get("action") or "buy",
            "count": row.get("contracts"),
            "price_cents": row.get("price_cents"),
        },
    }


def _ranker_paths(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("*.candidate_ranker.json"))


def _ranker_rows(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    generated_at = payload.get("generated_at")
    rows = payload.get("rows") if isinstance(payload, dict) else []
    out = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append({**row, "_artifact": path.name, "_generated_at": generated_at})
    return out


def _row_consensus_prob(row: dict[str, Any], side: str) -> float | None:
    consensus = row.get("consensus") if isinstance(row.get("consensus"), dict) else {}
    fair_prob = None
    raws = [consensus.get("fair_prob")]
    if str(row.get("signal_source") or "consensus") != "qual":
        raws.extend([row.get("model_prob"), row.get("sgp_adjusted_prob")])
    for raw in raws:
        fair_prob = _prob(raw)
        if fair_prob is not None:
            break
    if fair_prob is None:
        return None
    return round(1.0 - fair_prob, 6) if side == "no" else round(fair_prob, 6)


def closing_consensus(
    data_dir: Path,
    ticker: str,
    side: str,
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Find the latest available candidate-ranker consensus for this ticker."""
    close_at = _parse_dt((market or {}).get("close_time"))
    candidates = []
    for path in _ranker_paths(data_dir):
        for row in _ranker_rows(path):
            if row.get("ticker") != ticker:
                continue
            prob = _row_consensus_prob(row, side)
            if prob is None:
                continue
            generated_dt = _parse_dt(row.get("_generated_at"))
            if close_at and generated_dt and generated_dt > close_at:
                continue
            candidates.append((generated_dt or datetime.min.replace(tzinfo=timezone.utc), row, prob))

    if not candidates:
        return {
            "available": False,
            "reason": "no pre-close candidate-ranker consensus snapshot",
        }

    _, row, prob = sorted(candidates, key=lambda item: item[0])[-1]
    consensus = row.get("consensus") if isinstance(row.get("consensus"), dict) else {}
    return {
        "available": True,
        "source": "candidate-ranker",
        "artifact": row.get("_artifact"),
        "generated_at": row.get("_generated_at"),
        "prob": prob,
        "price_cents": round(prob * 100, 4),
        "book_count": consensus.get("book_count") or row.get("book_count"),
        "sources": consensus.get("sources") or row.get("model_prob_sources") or [],
        "raw_consensus": consensus,
    }


def settlement_record(
    order: dict[str, Any],
    market: dict[str, Any],
    resolution: dict[str, Any],
    consensus: dict[str, Any],
) -> dict[str, Any]:
    winning_side = resolution["winning_side"]
    outcome = int(winning_side == order["side"])
    contracts = float(order["contracts"])
    entry_price = float(order["entry_price_cents"])
    fee_cents = float(order.get("fee_cents") or 0.0)
    payout_cents = 100.0 * contracts if outcome else 0.0
    pnl_cents = payout_cents - (entry_price * contracts) - fee_cents
    unit_size_dollars = _num((order.get("intent") or {}).get("unit_size_dollars"))
    pnl_units = None
    if unit_size_dollars and unit_size_dollars > 0:
        pnl_units = round((pnl_cents / 100.0) / unit_size_dollars, 6)

    closing_prob = consensus.get("prob") if consensus.get("available") else None
    closing_cents = consensus.get("price_cents") if consensus.get("available") else None
    clv_cents = None
    beat_close = None
    if closing_cents is not None:
        if str(order.get("action") or "buy").lower() == "sell":
            clv_cents = round(entry_price - float(closing_cents), 4)
        else:
            clv_cents = round(float(closing_cents) - entry_price, 4)
        beat_close = clv_cents > 0

    model_prob = order.get("entry_model_prob")
    brier_entry = None
    if model_prob is not None:
        value = calibration.brier([{"prior_p": model_prob, "outcome": outcome}])
        brier_entry = round(value, 6) if math.isfinite(value) else None

    return {
        "client_order_id": order["client_order_id"],
        "game_id": order["game_id"],
        "mode": order["mode"],
        "ticker": order["ticker"],
        "side": order["side"],
        "action": order.get("action", "buy"),
        "contracts": contracts,
        "entry_price_cents": entry_price,
        "filled_at": order.get("filled_at"),
        "fill_source": order.get("fill_source"),
        "scenario_id": order.get("scenario_id"),
        "signal_source": order.get("signal_source") or "consensus",
        "confluence_verdict": order.get("confluence_verdict") or "none",
        "confluence": order.get("confluence") or {},
        "audited_at": utc_now(),
        "settled_at": resolution.get("settled_at"),
        "market_status": resolution.get("market_status"),
        "winning_side": winning_side,
        "outcome": outcome,
        "payout_cents": round(payout_cents, 4),
        "pnl_cents": round(pnl_cents, 4),
        "fee_cents": round(fee_cents, 4),
        "unit_size_dollars": unit_size_dollars,
        "pnl_units": pnl_units,
        "entry_model_prob": model_prob,
        "entry_market_prob": order.get("entry_market_prob"),
        "closing_consensus_prob": closing_prob,
        "closing_consensus_cents": closing_cents,
        "clv_cents": clv_cents,
        "beat_closing_consensus": beat_close,
        "brier_entry_model": brier_entry,
        "market": market,
        "closing_consensus": consensus,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    clv_records = [r for r in records if r.get("clv_cents") is not None]
    model_records = [r for r in records if r.get("entry_model_prob") is not None]
    brier_rows = [
        {"prior_p": r["entry_model_prob"], "outcome": r["outcome"]}
        for r in model_records
    ]
    brier_value = calibration.brier(brier_rows)
    mean_pred = (
        sum(float(r["entry_model_prob"]) for r in model_records) / len(model_records)
        if model_records else None
    )
    realized = (
        sum(int(r["outcome"]) for r in model_records) / len(model_records)
        if model_records else None
    )
    haircut = (
        round(realized / mean_pred, 3)
        if mean_pred and realized is not None and mean_pred > 0
        else None
    )
    return {
        "settled_count": len(records),
        "win_count": sum(1 for r in records if r.get("outcome") == 1),
        "loss_count": sum(1 for r in records if r.get("outcome") == 0),
        "clv_available_count": len(clv_records),
        "clv_beat_count": sum(1 for r in clv_records if r.get("beat_closing_consensus")),
        "clv_beat_rate": (
            round(
                sum(1 for r in clv_records if r.get("beat_closing_consensus"))
                / len(clv_records),
                4,
            )
            if clv_records else None
        ),
        "mean_clv_cents": (
            round(sum(float(r["clv_cents"]) for r in clv_records) / len(clv_records), 4)
            if clv_records else None
        ),
        "entry_model_brier": (
            round(brier_value, 6) if math.isfinite(brier_value) else None
        ),
        "realized_vs_predicted_haircut": haircut,
    }
