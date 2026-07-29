"""Odds conversion, de-vigging, and multi-book consensus math."""
from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any


BOOK_WEIGHTS = {
    "pinnacle": 3.0,
    "pinny": 3.0,
    "circa": 3.0,
    "betcris": 2.0,
    "novig": 2.0,
    "bovada": 1.0,
    "betrivers": 1.0,
    "draftkings": 1.0,
    "fanduel": 1.0,
    "fanatics": 1.0,
    "betmgm": 1.0,
    "caesars": 1.0,
    "espnbet": 1.0,
    "bet365": 1.0,
    "hardrock": 1.0,
    "parx": 1.0,
    "unknown": 0.5,
}

EXCLUDED_CONSENSUS_BOOKS = {"kalshi"}


def _num(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _key(raw: Any) -> str:
    return str(raw or "unknown").strip().lower().replace(" ", "")


def _norm(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "").lower() if ch.isalnum())


def implied_prob_american(odds: Any) -> float | None:
    value = _num(odds)
    if value is None or value == 0:
        return None
    if value > 0:
        return 100.0 / (value + 100.0)
    return abs(value) / (abs(value) + 100.0)


def implied_prob_decimal(odds: Any) -> float | None:
    value = _num(odds)
    if value is None or value <= 1:
        return None
    return 1.0 / value


def _format_key(raw: Any) -> str:
    return str(raw or "").strip().lower().replace("_", "").replace("-", "")


def implied_prob(raw: Any, *, price_format: str | None = None) -> float | None:
    value = _num(raw)
    if value is None:
        return None
    fmt = _format_key(price_format)
    if fmt in {"american", "us", "moneyline"}:
        return implied_prob_american(value)
    if fmt in {"decimal", "euro", "eu"}:
        return implied_prob_decimal(value)
    if 0 < value < 1:
        return value
    if 1 < value < 20:
        return implied_prob_decimal(value)
    return implied_prob_american(value)


def devig_multiplicative(probs: list[float]) -> list[float]:
    total = sum(p for p in probs if p > 0)
    if total <= 0:
        return []
    return [p / total for p in probs]


def devig_shin(probs: list[float], tol: float = 1e-7) -> list[float]:
    probs = [p for p in probs if p > 0]
    if len(probs) < 2:
        return devig_multiplicative(probs)
    total = sum(probs)
    if total <= 1:
        return devig_multiplicative(probs)

    def adjusted(z: float) -> list[float]:
        denom = max(2.0 * (1.0 - z), 1e-12)
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * (p * p / total)) - z) / denom
            for p in probs
        ]

    lo, hi = 0.0, 0.999999
    best = devig_multiplicative(probs)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        fair = adjusted(mid)
        s = sum(fair)
        best = fair
        if abs(s - 1.0) <= tol:
            return devig_multiplicative(fair)
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    if abs(sum(best) - 1.0) > 1e-4:
        return devig_multiplicative(probs)
    return devig_multiplicative(best)


def devig_power(probs: list[float], tol: float = 1e-7) -> list[float]:
    probs = [p for p in probs if p > 0]
    if len(probs) < 2:
        return devig_multiplicative(probs)
    lo, hi = 0.01, 10.0
    best = devig_multiplicative(probs)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        powered = [p ** mid for p in probs]
        s = sum(powered)
        best = devig_multiplicative(powered)
        if abs(s - 1.0) <= tol:
            return best
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    return best


def detect_outlier_books(
    book_probs: dict[str, float],
    median_tolerance: float = 0.08,
) -> tuple[dict[str, float], list[str]]:
    if len(book_probs) < 3:
        return dict(book_probs), []
    median = statistics.median(book_probs.values())
    survivors = {}
    excluded = []
    for book, prob in book_probs.items():
        if abs(prob - median) > median_tolerance:
            excluded.append(book)
        else:
            survivors[book] = prob
    return survivors or dict(book_probs), sorted(excluded)


def _book_name(row: dict[str, Any]) -> str:
    return _key(
        row.get("book")
        or row.get("sportsbook")
        or row.get("sportsBook")
        or row.get("bookmaker")
        or row.get("bookmakerKey")
        or row.get("bookID")
        or row.get("provider")
    )


def _target_matches(row: dict[str, Any], target_name: str | None) -> bool:
    if not target_name:
        return False
    target = _norm(target_name)
    names = [
        row.get("name"),
        row.get("selection"),
        row.get("side"),
        row.get("participant"),
        row.get("team"),
        row.get("sideID"),
        row.get("side_id"),
    ]
    return any(_norm(name) == target for name in names if name is not None)


def _price_value(row: dict[str, Any]) -> Any:
    for key in ("price", "odds", "americanOdds", "decimalOdds"):
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _row_price_format(row: dict[str, Any]) -> str | None:
    for key in ("price_format", "odds_format", "oddsFormat", "format"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    if row.get("decimalOdds") not in (None, "") and row.get("americanOdds") in (None, ""):
        return "decimal"
    if row.get("americanOdds") not in (None, "") and row.get("decimalOdds") in (None, ""):
        return "american"
    return None


def _point_key(row: dict[str, Any]) -> str:
    point = _num(row.get("point") or row.get("line") or row.get("spread") or row.get("total"))
    return f"{point:.4f}" if point is not None else ""


def _outcome_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _norm(row.get("market") or row.get("marketName") or row.get("type") or row.get("oddID")),
        _point_key(row),
        _norm(
            row.get("name")
            or row.get("selection")
            or row.get("side")
            or row.get("participant")
            or row.get("team")
            or row.get("sideID")
            or row.get("side_id")
        ),
    )


def _freshness_rank(row: dict[str, Any]) -> float:
    for key in ("last_update_ms", "lastUpdatedMs", "last_updated_ms"):
        value = _num(row.get(key))
        if value is not None:
            return value
    for key in ("last_update", "lastUpdatedAt", "last_updated", "updated_at", "captured_at"):
        value = row.get(key)
        if value in (None, ""):
            continue
        numeric = _num(value)
        if numeric is not None:
            return numeric
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return float(datetime.fromisoformat(text).timestamp())
        except ValueError:
            continue
    return 0.0


def _dedupe_book_outcomes(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], tuple[tuple[float, int], dict[str, Any]]] = {}
    for index, row in enumerate(outcomes):
        key = _outcome_key(row)
        rank = (_freshness_rank(row), index)
        current = latest.get(key)
        if current is None or rank >= current[0]:
            latest[key] = (rank, row)
    selected = sorted(latest.values(), key=lambda item: item[0][1])
    return [row for _, row in selected]


def consensus_prob(
    rows: list[dict[str, Any]],
    weights: dict[str, float] | None = None,
    *,
    target_name: str | None = None,
    method: str = "shin",
) -> dict[str, Any]:
    weights = weights or BOOK_WEIGHTS
    by_book: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        book = _book_name(row)
        if book in EXCLUDED_CONSENSUS_BOOKS:
            continue
        prob = implied_prob(_price_value(row), price_format=_row_price_format(row))
        if prob is None or not 0 < prob < 1:
            continue
        by_book.setdefault(book, []).append({**row, "_implied_prob": prob})

    book_probs: dict[str, float] = {}
    book_providers: dict[str, list[str]] = {}
    sources = []
    for book, outcomes in sorted(by_book.items()):
        outcomes = _dedupe_book_outcomes(outcomes)
        probs = [float(row["_implied_prob"]) for row in outcomes]
        if len(probs) >= 2 and method == "power":
            fair = devig_power(probs)
        elif len(probs) >= 2 and method == "shin":
            fair = devig_shin(probs)
        else:
            fair = devig_multiplicative(probs)
        if not fair:
            continue
        target_index = 0
        if target_name:
            matches = [i for i, row in enumerate(outcomes) if _target_matches(row, target_name)]
            if not matches:
                continue
            target_index = matches[0]
        elif len(outcomes) != 1:
            continue
        if target_index >= len(fair):
            continue
        book_probs[book] = fair[target_index]
        book_providers[book] = sorted({
            str(row.get("provider"))
            for row in outcomes
            if row.get("provider") not in (None, "")
        })
        sources.append(book)

    survivors, excluded = detect_outlier_books(book_probs)
    if not survivors:
        return {
            "fair_prob": None,
            "book_count": 0,
            "sources": [],
            "providers": [],
            "excluded_books": excluded,
            "disagreement_std": None,
        }

    weighted_total = 0.0
    weight_sum = 0.0
    for book, prob in survivors.items():
        weight = float(weights.get(book, weights.get("unknown", 0.5)))
        weighted_total += prob * weight
        weight_sum += weight
    values = list(survivors.values())
    providers = sorted({
        provider
        for book in survivors
        for provider in book_providers.get(book, [])
    })
    return {
        "fair_prob": weighted_total / weight_sum if weight_sum else None,
        "book_count": len(survivors),
        "sources": sorted(survivors),
        "providers": providers,
        "excluded_books": excluded,
        "disagreement_std": statistics.pstdev(values) if len(values) > 1 else 0.0,
    }
