"""Pure confluence rules for consensus and qualitative signals."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_AGREE_DELTA = 0.05
DEFAULT_EDGE_BONUS = 0.01
DEFAULT_VETO_DELTA = 0.08
MIN_EFFECTIVE_BASE = 0.02
VETO_REASON = "qual disagrees with consensus"


@dataclass(frozen=True)
class ConfluenceRecord:
    consensus_fair_prob: float | None
    qual_prob: float | None
    qual_confidence: float | None
    delta: float | None
    verdict: str
    applies: bool
    edge_bonus: float
    base_min_edge: float | None
    effective_base_min_edge: float | None
    veto: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _prob(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1:
        value /= 100.0
    if 0 <= value <= 1:
        return value
    return None


def _setting(settings: Any, name: str, default: float) -> float:
    try:
        return float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return float(default)


def applies_to_mode(settings: Any) -> bool:
    mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    return mode in {"paper", "demo"}


def effective_base_for_agreement(settings: Any, base_min_edge: float) -> float:
    return round(float(base_min_edge), 5)


def evaluate_confluence(
    *,
    consensus: dict[str, Any],
    qual_signal: dict[str, Any] | None,
    price_prob: float | None,
    settings: Any,
    base_min_edge: float,
    exact_match: bool,
) -> ConfluenceRecord:
    fair = _prob(consensus.get("fair_prob"))
    qual = _prob((qual_signal or {}).get("qual_prob"))
    confidence = _prob((qual_signal or {}).get("confidence"))
    book_count = int(consensus.get("book_count") or 0)
    mode_applies = applies_to_mode(settings)
    no_bonus_base = round(float(base_min_edge), 5)

    if not exact_match or book_count < 2 or fair is None or qual is None:
        return ConfluenceRecord(
            consensus_fair_prob=fair,
            qual_prob=qual,
            qual_confidence=confidence,
            delta=None,
            verdict="none",
            applies=False,
            edge_bonus=0.0,
            base_min_edge=no_bonus_base,
            effective_base_min_edge=no_bonus_base,
            veto=False,
            reason="missing exact consensus/qual overlap",
        )

    delta = round(qual - fair, 6)
    agree_delta = abs(_setting(settings, "confluence_agree_delta", DEFAULT_AGREE_DELTA))
    veto_delta = abs(_setting(settings, "confluence_veto_delta", DEFAULT_VETO_DELTA))
    consensus_edge = None if price_prob is None else fair - float(price_prob)
    qual_edge = None if price_prob is None else qual - float(price_prob)
    verdict = "neutral"
    reason = "qual/consensus overlap is neutral"
    if (
        consensus_edge is not None
        and qual_edge is not None
        and consensus_edge > 0
        and qual_edge > 0
        and abs(delta) <= agree_delta
    ):
        verdict = "agree"
        reason = "qual agrees with consensus"
    elif (
        confidence is not None
        and confidence >= 0.60
        and consensus_edge is not None
        and consensus_edge != 0
        and delta * consensus_edge < 0
        and abs(delta) >= veto_delta
    ):
        verdict = "disagree"
        reason = VETO_REASON

    edge_bonus = 0.0
    effective_base = no_bonus_base
    return ConfluenceRecord(
        consensus_fair_prob=round(fair, 6),
        qual_prob=round(qual, 6),
        qual_confidence=None if confidence is None else round(confidence, 6),
        delta=delta,
        verdict=verdict,
        applies=mode_applies and verdict in {"agree", "disagree"},
        edge_bonus=edge_bonus,
        base_min_edge=no_bonus_base,
        effective_base_min_edge=effective_base,
        veto=mode_applies and verdict == "disagree",
        reason=reason,
    )
