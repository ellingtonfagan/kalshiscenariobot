"""Pure edge evaluation for matched Kalshi sportsbook candidates."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .fees import fee_details

DEFAULT_MAX_PLAUSIBLE_EDGE = 0.15
IMPLAUSIBLE_EDGE_BLOCKER = "edge exceeds plausible maximum; likely identity/line mismatch"


@dataclass(frozen=True)
class ExecutablePrice:
    side: str
    bid_cents: int | None
    ask_cents: int | None
    spread_cents: int | None
    fillable_contracts: int
    vwap_cents: float | None
    captured_at: str | None
    liquidity_role: str = "taker"

    @property
    def maker(self) -> bool:
        return self.liquidity_role == "maker"

    @property
    def price_cents(self) -> int | None:
        if self.maker:
            price = self.bid_cents
        else:
            price = self.vwap_cents if self.vwap_cents is not None else self.ask_cents
        return int(round(price)) if price is not None else None

    @property
    def price_prob(self) -> float | None:
        price = self.price_cents
        return price / 100.0 if price is not None else None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["price_cents"] = self.price_cents
        payload["price_prob"] = self.price_prob
        return payload

    @classmethod
    def from_orderbook_metrics(
        cls,
        metrics: dict[str, Any],
        side: str = "yes",
        *,
        maker: bool = False,
    ) -> "ExecutablePrice":
        side_metrics = metrics.get(side) if isinstance(metrics, dict) else None
        side_metrics = side_metrics if isinstance(side_metrics, dict) else {}
        return cls(
            side=side,
            bid_cents=side_metrics.get("best_bid_cents"),
            ask_cents=side_metrics.get("best_ask_cents"),
            spread_cents=side_metrics.get("spread_cents"),
            fillable_contracts=int(side_metrics.get("fillable_contracts") or 0),
            vwap_cents=side_metrics.get("vwap_cents"),
            captured_at=side_metrics.get("captured_at"),
            liquidity_role="maker" if maker else "taker",
        )


@dataclass(frozen=True)
class EdgeCandidate:
    ticker: str
    side: str
    model_prob: float | None
    model_prob_sources: list[str]
    executable_price: dict[str, Any]
    raw_edge: float | None
    net_edge: float | None
    edge: float | None
    expected_fee_cents: float | None
    expected_fee_prob: float | None
    liquidity_role: str
    required_edge: float
    passes_edge: bool
    time_to_close_seconds: int | None
    blockers: list[str]
    match_type: str
    book_count: int
    disagreement_std: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def time_to_close_seconds(close_time: Any, now: datetime | None = None) -> int | None:
    parsed = _parse_ts(close_time)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return int((parsed - now).total_seconds())


def required_edge(
    base_min_edge: float,
    disagreement_std: float | None,
    book_count: int,
    spread_cents: int | None,
) -> float:
    edge = float(base_min_edge)
    if book_count <= 1:
        edge += 0.03
    elif book_count < 4:
        edge += 0.015
    if disagreement_std is not None:
        edge += min(float(disagreement_std) * 0.75, 0.08)
    if spread_cents is None:
        edge += 0.03
    else:
        edge += min(max(int(spread_cents), 0) / 100.0 * 0.25, 0.05)
    return round(edge, 5)


def _stale(captured_at: str | None, max_age_seconds: int,
           now: datetime | None = None) -> bool:
    parsed = _parse_ts(captured_at)
    if parsed is None:
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - parsed).total_seconds() > max_age_seconds


def _max_plausible_edge(settings: Any) -> float:
    try:
        value = float(getattr(settings, "max_plausible_edge", DEFAULT_MAX_PLAUSIBLE_EDGE))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PLAUSIBLE_EDGE
    return max(value, 0.0)


def evaluate_market(
    identity_match: dict[str, Any],
    consensus: dict[str, Any],
    executable: ExecutablePrice,
    settings: Any,
    *,
    base_min_edge: float,
    now: datetime | None = None,
    require_consensus: bool = True,
    require_exact_match: bool = True,
) -> EdgeCandidate:
    model_prob = consensus.get("fair_prob")
    if executable.side == "no" and model_prob is not None:
        model_prob = round(1.0 - float(model_prob), 6)
    price_prob = executable.price_prob
    book_count = int(consensus.get("book_count") or 0)
    disagreement = consensus.get("disagreement_std")
    required = required_edge(
        float(base_min_edge),
        float(disagreement) if disagreement is not None else None,
        book_count if require_consensus else 4,
        executable.spread_cents,
    )
    edge = (
        float(model_prob) - float(price_prob)
        if model_prob is not None and price_prob is not None
        else None
    )
    fee = fee_details(executable.price_cents, maker=executable.maker, settings=settings)
    fee_prob = fee.get("expected_fee_prob")
    net_edge = (
        round(float(edge) - float(fee_prob), 6)
        if edge is not None and fee_prob is not None
        else edge
    )
    blockers = []
    match_type = str(identity_match.get("match_type") or "none")
    if require_exact_match and match_type != "exact":
        blockers.append("identity match is not exact")
    if model_prob is None:
        blockers.append("model probability unavailable from sportsbook consensus")
    if require_consensus and book_count < 2:
        blockers.append("insufficient sportsbook consensus")
    if executable.price_cents is None:
        blockers.append("missing executable Kalshi price")
    if not executable.maker and executable.fillable_contracts <= 0:
        blockers.append("no fillable contracts at observed limit")
    if executable.spread_cents is None or executable.spread_cents > int(settings.max_spread_cents):
        blockers.append("spread missing or wider than configured max")
    if _stale(executable.captured_at, int(settings.stale_market_seconds), now):
        blockers.append("stale executable orderbook data")
    if net_edge is None:
        blockers.append("missing edge")
    elif net_edge < required:
        blockers.append("edge below dynamic required edge")
    if edge is not None and edge > _max_plausible_edge(settings):
        blockers.append(IMPLAUSIBLE_EDGE_BLOCKER)
    passes = not blockers
    return EdgeCandidate(
        ticker=str(identity_match.get("ticker") or ""),
        side=executable.side,
        model_prob=model_prob,
        model_prob_sources=list(consensus.get("sources") or []),
        executable_price=executable.as_dict(),
        raw_edge=round(edge, 5) if edge is not None else None,
        net_edge=round(net_edge, 5) if net_edge is not None else None,
        edge=round(net_edge, 5) if net_edge is not None else None,
        expected_fee_cents=fee.get("expected_fee_cents"),
        expected_fee_prob=fee.get("expected_fee_prob"),
        liquidity_role=str(fee.get("liquidity_role") or executable.liquidity_role),
        required_edge=required,
        passes_edge=passes,
        time_to_close_seconds=time_to_close_seconds(identity_match.get("close_time"), now),
        blockers=blockers,
        match_type=match_type,
        book_count=book_count,
        disagreement_std=disagreement,
    )


def rank_candidates(candidates: list[EdgeCandidate]) -> list[EdgeCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            not item.passes_edge,
            -(item.net_edge if item.net_edge is not None else -999.0),
            item.required_edge,
        ),
    )
