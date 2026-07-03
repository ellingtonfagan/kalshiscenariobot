"""Kalshi YES/NO order book model.

Kalshi returns bid ladders for YES and NO. Asks are implied from the opposite
side: a NO bid at 28c is a YES ask at 72c.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .research import utc_now


def _price_cents(raw: Any) -> int:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    if value <= 1:
        value *= 100
    return int(round(value))


def _contracts(raw: Any) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


def _levels(raw: list | None) -> dict[int, int]:
    out: dict[int, int] = {}
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = _price_cents(row[0])
        qty = _contracts(row[1])
        if 0 < price < 100 and qty > 0:
            out[price] = qty
    return out


@dataclass
class OrderBook:
    ticker: str
    yes: dict[int, int] = field(default_factory=dict)
    no: dict[int, int] = field(default_factory=dict)
    captured_at: str = field(default_factory=utc_now)
    source: str = "kalshi"

    @classmethod
    def from_snapshot(cls, ticker: str, msg: dict[str, Any],
                      source: str = "kalshi") -> "OrderBook":
        book = cls(ticker=ticker, source=source)
        book.apply_snapshot(msg)
        return book

    @classmethod
    def from_kalshi_response(cls, ticker: str, response: dict[str, Any],
                             source: str = "kalshi-rest") -> "OrderBook":
        raw = response.get("orderbook_fp") or response.get("orderbook") or response
        return cls.from_snapshot(ticker, {
            "yes": raw.get("yes") or raw.get("yes_dollars") or raw.get("yes_bids"),
            "no": raw.get("no") or raw.get("no_dollars") or raw.get("no_bids"),
        }, source=source)

    def apply_snapshot(self, msg: dict[str, Any]) -> None:
        self.yes = _levels(msg.get("yes") or msg.get("yes_dollars") or msg.get("yes_bids"))
        self.no = _levels(msg.get("no") or msg.get("no_dollars") or msg.get("no_bids"))
        self.captured_at = utc_now()

    def apply_delta(self, msg: dict[str, Any]) -> None:
        side_name = str(msg.get("side", "")).lower()
        side = self.yes if side_name == "yes" else self.no if side_name == "no" else None
        if side is None:
            return
        price = _price_cents(msg.get("price"))
        delta = _contracts(msg.get("delta"))
        if not 0 < price < 100 or delta == 0:
            return
        new_qty = side.get(price, 0) + delta
        if new_qty <= 0:
            side.pop(price, None)
        else:
            side[price] = new_qty
        self.captured_at = utc_now()

    def bids(self, side: str = "yes") -> list[tuple[int, int]]:
        book = self.yes if side == "yes" else self.no
        return sorted(book.items(), reverse=True)

    def asks(self, side: str = "yes") -> list[tuple[int, int]]:
        other = self.no if side == "yes" else self.yes
        return sorted(((100 - price, qty) for price, qty in other.items()))

    def best_bid(self, side: str = "yes") -> int | None:
        levels = self.bids(side)
        return levels[0][0] if levels else None

    def best_ask(self, side: str = "yes") -> int | None:
        levels = self.asks(side)
        return levels[0][0] if levels else None

    def spread(self, side: str = "yes") -> int | None:
        bid, ask = self.best_bid(side), self.best_ask(side)
        return (ask - bid) if bid is not None and ask is not None else None

    def depth_at_limit(self, side: str, limit_cents: int) -> int:
        return sum(qty for price, qty in self.asks(side) if price <= limit_cents)

    def vwap_to_buy(self, side: str, contracts: int,
                    limit_cents: int | None = None) -> tuple[float | None, int]:
        remaining = int(contracts)
        if remaining <= 0:
            return None, 0
        filled = 0
        notional = 0
        for price, qty in self.asks(side):
            if limit_cents is not None and price > limit_cents:
                break
            take = min(remaining, qty)
            filled += take
            notional += take * price
            remaining -= take
            if remaining == 0:
                break
        if filled == 0:
            return None, 0
        return notional / filled, filled

    def metrics(self, side: str = "yes", contracts: int = 1,
                limit_cents: int | None = None) -> dict[str, Any]:
        bid, ask = self.best_bid(side), self.best_ask(side)
        spread = self.spread(side)
        limit = limit_cents if limit_cents is not None else ask
        depth = self.depth_at_limit(side, int(limit)) if limit is not None else 0
        vwap, fillable = self.vwap_to_buy(side, contracts, int(limit)) if limit is not None else (None, 0)
        top_bid_qty = self.bids(side)[0][1] if self.bids(side) else 0
        top_ask_qty = self.asks(side)[0][1] if self.asks(side) else 0
        return {
            "ticker": self.ticker,
            "side": side,
            "captured_at": self.captured_at,
            "best_bid_cents": bid,
            "best_ask_cents": ask,
            "spread_cents": spread,
            "top_bid_contracts": top_bid_qty,
            "top_ask_contracts": top_ask_qty,
            "limit_cents": limit,
            "depth_at_limit_contracts": depth,
            "requested_contracts": contracts,
            "fillable_contracts": fillable,
            "vwap_cents": round(vwap, 4) if vwap is not None else None,
            "slippage_cents": round(vwap - ask, 4) if vwap is not None and ask is not None else None,
            "source": self.source,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "captured_at": self.captured_at,
            "source": self.source,
            "yes": sorted(self.yes.items(), reverse=True),
            "no": sorted(self.no.items(), reverse=True),
        }
