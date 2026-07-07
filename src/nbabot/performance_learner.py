"""Broad-slate performance learning by market family.

This generalizes the scenario calibration loop to settled execution records:
bucket historical fills by deterministic market family, summarize realized
performance, and only mark a family validated after enough settlement evidence.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from . import calibration
from .market_identity import KALSHI_SERIES_SPORTS, classify_market_type
from .research import ResearchStore, utc_now
from .settlement_audit import summarize


VALIDATION_MIN_SETTLED = 100
VALIDATION_MIN_CLV_BEAT_RATE = 0.60
BRIER_MIN_IMPROVEMENT = 0.005
DEFAULT_MAX_PRIOR_SHIFT = 0.05


def _loads(raw: Any, default: Any = None) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _prob(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1:
        value /= 100.0
    if value < 0 or value > 1:
        return None
    return value


def _outcome(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 1} else None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _token(raw: Any) -> str | None:
    text = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or None


def _family_text(raw: Any) -> str | None:
    text = str(raw or "").strip().lower().replace("-", "_")
    text = re.sub(r"[^a-z0-9_ ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _deep_get(doc: dict[str, Any], path: str) -> Any:
    value: Any = doc
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _payloads(row: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = [row]
    for key in ("record_json", "market_json", "consensus_json"):
        parsed = _loads(row.get(key), {})
        if isinstance(parsed, dict):
            payloads.append(parsed)
            for nested_key in ("market", "closing_consensus", "consensus"):
                nested = parsed.get(nested_key)
                if isinstance(nested, dict):
                    payloads.append(nested)
    for nested_key in ("market", "closing_consensus", "consensus"):
        nested = row.get(nested_key)
        if isinstance(nested, dict):
            payloads.append(nested)
    return payloads


def _first(payloads: Iterable[dict[str, Any]], *paths: str) -> Any:
    for payload in payloads:
        for path in paths:
            value = _deep_get(payload, path)
            if value not in (None, "", []):
                return value
    return None


def _series_from_payloads(payloads: list[dict[str, Any]]) -> str | None:
    raw = _first(payloads, "series_ticker", "series", "market.series_ticker", "market.series")
    if raw:
        return str(raw).split("-", 1)[0].upper()
    for key in ("ticker", "market_ticker", "event_ticker"):
        raw = _first(payloads, key, f"market.{key}")
        if raw:
            return str(raw).split("-", 1)[0].upper()
    return None


def _sport_from_series(series: str | None) -> str | None:
    if not series:
        return None
    for prefix, sport in sorted(KALSHI_SERIES_SPORTS.items(), key=lambda row: len(row[0]), reverse=True):
        if series.startswith(prefix):
            return sport
    return None


def _market_type_from_series(series: str | None) -> str | None:
    if not series:
        return None
    series = series.upper()
    if series.startswith("KXMVE"):
        return "composite"
    if series.endswith(("GAME", "MATCH", "FIGHT")):
        return "moneyline"
    if series.endswith("ADVANCE"):
        return "team_advances"
    if series.endswith("SPREAD"):
        return "spread"
    if series.endswith("TOTAL"):
        return "total"
    if series.endswith("BTTS"):
        return "btts"
    if series.endswith("GOAL"):
        return "team_total"
    return None


def _market_type_from_configured_market(raw: Any) -> str | None:
    text = str(raw or "").lower()
    suffixes = {
        "_points": "player_points",
        "_rebounds": "player_rebounds",
        "_assists": "player_assists",
        "_threes": "player_threes",
        "_minutes": "player_minutes",
        "_win": "moneyline",
        "_cover": "spread",
        "_total": "total",
    }
    for suffix, market_type in suffixes.items():
        if text.endswith(suffix):
            return market_type
    return None


def _is_composite(payloads: list[dict[str, Any]], series: str | None) -> bool:
    if _market_type_from_series(series) == "composite":
        return True
    if bool(_first(payloads, "is_composite", "market.is_composite")):
        return True
    if _first(payloads, "mve_collection_ticker", "market.mve_collection_ticker"):
        return True
    legs = _first(payloads, "mve_selected_legs", "market.mve_selected_legs", "identity.legs", "market_identity.legs")
    if isinstance(legs, list) and legs:
        return True
    market_type = _first(payloads, "market_type", "identity.market_type", "market_identity.market_type")
    return str(market_type or "").lower() == "composite"


def market_family(row: dict[str, Any], default_sport: str | None = None) -> str:
    """Return a deterministic broad-slate family key such as ``mlb moneyline``."""
    payloads = _payloads(row)
    explicit = _first(payloads, "market_family", "performance_family")
    if explicit:
        return _family_text(explicit) or "unknown unknown"

    series = _series_from_payloads(payloads)
    if _is_composite(payloads, series):
        return "composite"

    sport_raw = _first(
        payloads,
        "sport",
        "market.sport",
        "market_identity.sport",
        "identity.sport",
    )
    if isinstance(sport_raw, list):
        sport_raw = sport_raw[0] if sport_raw else None
    if sport_raw is None:
        sport_keys = _first(payloads, "sport_keys", "market.sport_keys")
        if isinstance(sport_keys, list):
            sport_raw = sport_keys[0] if sport_keys else None
    sport = _token(sport_raw) or _sport_from_series(series) or _token(default_sport) or "unknown_sport"

    market_type = _first(
        payloads,
        "market_identity.market_type",
        "identity.market_type",
        "market_type",
        "market.market_type",
    )
    market_type = _token(market_type)
    if market_type in {None, "unknown", "binary"}:
        market_type = _market_type_from_series(series)
    if market_type in {None, "unknown", "binary"}:
        market_type = _market_type_from_configured_market(_first(payloads, "market"))
    if market_type in {None, "unknown", "binary"}:
        title_or_market = _first(payloads, "title", "market.title", "market")
        market_type = classify_market_type(title_or_market, sport)
    market_type = _token(market_type) or "unknown"

    if market_type == "composite":
        return "composite"
    return f"{sport} {market_type}"


def _confidence(settled_n: int, min_validated_n: int) -> str:
    if settled_n >= min_validated_n:
        return "validation_sample"
    if settled_n >= max(int(min_validated_n * 0.5), 1):
        return "building"
    if settled_n >= 20:
        return "thin"
    if settled_n > 0:
        return "very_thin"
    return "none"


def _brier_comparison(records: list[dict[str, Any]]) -> dict[str, Any]:
    model_rows = []
    market_rows = []
    for record in records:
        outcome = _outcome(record.get("outcome"))
        model_prob = _prob(record.get("entry_model_prob"))
        market_prob = _prob(record.get("entry_market_prob"))
        if outcome is None or model_prob is None or market_prob is None:
            continue
        model_rows.append({"prior_p": model_prob, "outcome": outcome})
        market_rows.append({"prior_p": market_prob, "outcome": outcome})

    model_brier = calibration.brier(model_rows)
    market_brier = calibration.brier(market_rows)
    model_value = round(model_brier, 6) if math.isfinite(model_brier) else None
    market_value = round(market_brier, 6) if math.isfinite(market_brier) else None
    improvement = (
        round(market_value - model_value, 6)
        if model_value is not None and market_value is not None
        else None
    )
    improvement_pct = (
        round(improvement / market_value, 6)
        if improvement is not None and market_value not in (None, 0)
        else None
    )
    return {
        "brier_comparison_count": len(model_rows),
        "entry_model_brier_comparable": model_value,
        "market_baseline_brier": market_value,
        "brier_improvement_vs_market": improvement,
        "brier_improvement_pct_vs_market": improvement_pct,
    }


def _bias(records: list[dict[str, Any]]) -> float | None:
    rows = []
    for record in records:
        outcome = _outcome(record.get("outcome"))
        model_prob = _prob(record.get("entry_model_prob"))
        if outcome is not None and model_prob is not None:
            rows.append(model_prob - outcome)
    if not rows:
        return None
    return round(sum(rows) / len(rows), 6)


def _family_report(
    family: str,
    records: list[dict[str, Any]],
    *,
    min_validated_n: int,
    min_clv_beat_rate: float,
    min_brier_improvement: float,
) -> dict[str, Any]:
    summary = summarize(records)
    brier = _brier_comparison(records)
    settled_n = int(summary.get("settled_count") or 0)
    clv_n = int(summary.get("clv_available_count") or 0)
    brier_n = int(brier["brier_comparison_count"] or 0)
    clv_rate = summary.get("clv_beat_rate")
    brier_improvement = brier.get("brier_improvement_vs_market")
    brier_better = (
        brier_improvement is not None
        and brier_improvement >= min_brier_improvement
    )

    blockers = []
    if settled_n < min_validated_n:
        blockers.append(f"settled sample n={settled_n} below validation minimum {min_validated_n}")
    if clv_n < min_validated_n:
        blockers.append(f"CLV sample n={clv_n} below validation minimum {min_validated_n}")
    if clv_rate is None:
        blockers.append("CLV beat rate unavailable")
    elif clv_rate < min_clv_beat_rate:
        blockers.append(f"CLV beat rate {clv_rate:.3f} below validation minimum {min_clv_beat_rate:.3f}")
    if brier_n < min_validated_n:
        blockers.append(f"Brier comparison sample n={brier_n} below validation minimum {min_validated_n}")
    if not brier_better:
        blockers.append(
            "model Brier is not meaningfully better than market-implied baseline"
        )

    validated = not blockers
    return {
        "market_family": family,
        "settled_count": settled_n,
        "win_count": summary.get("win_count", 0),
        "loss_count": summary.get("loss_count", 0),
        "clv_available_count": clv_n,
        "clv_beat_count": summary.get("clv_beat_count", 0),
        "clv_beat_rate": clv_rate,
        "mean_clv_cents": summary.get("mean_clv_cents"),
        "entry_model_brier": summary.get("entry_model_brier"),
        **brier,
        "brier_meaningfully_better_than_market": brier_better,
        "mean_model_minus_outcome_bias": _bias(records),
        "realized_vs_predicted_haircut": summary.get("realized_vs_predicted_haircut"),
        "confidence": _confidence(settled_n, min_validated_n),
        "validated": validated,
        "market_type_verdict": "validated" if validated else "not_yet_validated",
        "validation_blockers": blockers,
    }


def empty_family_report(family: str, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    thresholds = thresholds or {
        "min_validated_n": VALIDATION_MIN_SETTLED,
        "min_clv_beat_rate": VALIDATION_MIN_CLV_BEAT_RATE,
        "min_brier_improvement": BRIER_MIN_IMPROVEMENT,
    }
    min_n = int(thresholds.get("min_validated_n") or VALIDATION_MIN_SETTLED)
    return {
        "market_family": family,
        "settled_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "clv_available_count": 0,
        "clv_beat_count": 0,
        "clv_beat_rate": None,
        "mean_clv_cents": None,
        "entry_model_brier": None,
        "brier_comparison_count": 0,
        "entry_model_brier_comparable": None,
        "market_baseline_brier": None,
        "brier_improvement_vs_market": None,
        "brier_improvement_pct_vs_market": None,
        "brier_meaningfully_better_than_market": False,
        "mean_model_minus_outcome_bias": None,
        "realized_vs_predicted_haircut": None,
        "confidence": "none",
        "validated": False,
        "market_type_verdict": "not_yet_validated",
        "validation_blockers": [
            f"settled sample n=0 below validation minimum {min_n}",
            f"CLV sample n=0 below validation minimum {min_n}",
            f"Brier comparison sample n=0 below validation minimum {min_n}",
        ],
    }


def suggest_overrides(
    learning: dict[str, Any],
    *,
    min_family_n: int | None = None,
    max_prior_shift: float = DEFAULT_MAX_PRIOR_SHIFT,
) -> dict[str, Any]:
    """Return conservative, shrink-only market-family calibration suggestions."""
    min_family_n = int(min_family_n or VALIDATION_MIN_SETTLED)
    overrides: dict[str, Any] = {
        "version": 1,
        "source": "performance_learner.learn",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_family_n": min_family_n,
        "max_prior_shift": max_prior_shift,
        "market_family_prior_multipliers": {},
        "market_family_haircut": {},
        "notes": [],
    }
    for family, row in (learning.get("families") or {}).items():
        n = int(row.get("settled_count") or 0)
        try:
            haircut = float(row.get("realized_vs_predicted_haircut"))
        except (TypeError, ValueError):
            continue
        if n < min_family_n or haircut >= 1.0:
            continue
        shift = _clamp(1.0 - haircut, 0.0, max_prior_shift)
        multiplier = round(1.0 - shift, 5)
        overrides["market_family_prior_multipliers"][family] = multiplier
        overrides["market_family_haircut"][family] = round(_clamp(haircut, 0.05, 1.0), 5)
        overrides["notes"].append(
            f"{family}: learned haircut {haircut:.3f} over n={n}; shrink multiplier {multiplier:.3f}"
        )
    return overrides


def learn(
    records: Iterable[dict[str, Any]],
    *,
    default_sport: str | None = None,
    min_validated_n: int = VALIDATION_MIN_SETTLED,
    min_clv_beat_rate: float = VALIDATION_MIN_CLV_BEAT_RATE,
    min_brier_improvement: float = BRIER_MIN_IMPROVEMENT,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    normalized = [dict(record) for record in records]
    for record in normalized:
        family = market_family(record, default_sport=default_sport)
        buckets.setdefault(family, []).append(record)

    families = {
        family: _family_report(
            family,
            rows,
            min_validated_n=min_validated_n,
            min_clv_beat_rate=min_clv_beat_rate,
            min_brier_improvement=min_brier_improvement,
        )
        for family, rows in sorted(buckets.items())
    }
    payload: dict[str, Any] = {
        "version": 1,
        "source": "settlement_records",
        "generated_at": utc_now(),
        "settled_count": len(normalized),
        "family_count": len(families),
        "validation_thresholds": {
            "min_validated_n": min_validated_n,
            "min_clv_beat_rate": min_clv_beat_rate,
            "min_brier_improvement": min_brier_improvement,
            "baseline": "entry_market_prob",
        },
        "families": families,
        "notes": [
            "Validation is based only on settled execution records.",
            "Brier validation compares entry_model_prob against entry_market_prob on the same rows.",
            "Suggested overrides are shrink-only; they never raise a model probability or haircut.",
        ],
    }
    payload["suggested_overrides"] = suggest_overrides(payload, min_family_n=min_validated_n)
    return payload


def learn_from_store(
    store: ResearchStore,
    *,
    default_sport: str | None = None,
) -> dict[str, Any]:
    return learn(store.list_settlement_records(), default_sport=default_sport)


def family_performance(learning: dict[str, Any], family: str) -> dict[str, Any]:
    families = learning.get("families") or {}
    row = families.get(family)
    if isinstance(row, dict):
        return row
    return empty_family_report(family, learning.get("validation_thresholds"))


def annotate_candidate(
    row: dict[str, Any],
    learning: dict[str, Any],
    *,
    default_sport: str | None = None,
) -> dict[str, Any]:
    family = market_family(row, default_sport=default_sport)
    performance = family_performance(learning, family)
    row["market_family"] = family
    row["performance"] = performance
    row["validated"] = bool(performance.get("validated"))
    row["market_type_verdict"] = str(
        performance.get("market_type_verdict") or "not_yet_validated"
    )
    return row
