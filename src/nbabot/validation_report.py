"""Validation reporting for broad-slate live eligibility."""
from __future__ import annotations

from typing import Any, Iterable

from . import calibration
from .performance_learner import (
    BRIER_MIN_IMPROVEMENT,
    VALIDATION_MIN_CLV_BEAT_RATE,
    VALIDATION_MIN_SETTLED,
    market_family,
)


def _float(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _prob(raw: Any) -> float | None:
    value = _float(raw)
    if value is None:
        return None
    if value > 1:
        value /= 100.0
    return value if 0 <= value <= 1 else None


def _outcome(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 1} else None


def concentration_diagnostics(
    records: Iterable[dict[str, Any]],
    *,
    max_winner_share: float = 0.50,
) -> dict[str, Any]:
    rows = list(records)
    pnl_values = [_float(row.get("pnl_cents")) or 0.0 for row in rows]
    total = round(sum(pnl_values), 4)
    winners = [
        (idx, pnl)
        for idx, pnl in enumerate(pnl_values)
        if pnl > 0
    ]
    if not winners:
        return {
            "total_pnl_cents": total,
            "largest_winner_pnl_cents": None,
            "largest_winner_client_order_id": None,
            "largest_winner_ticker": None,
            "pnl_ex_largest_winner_cents": total,
            "largest_winner_share_of_total_pnl": None,
            "concentration_flag": False,
            "concentration_threshold": max_winner_share,
        }
    idx, largest = max(winners, key=lambda item: item[1])
    share = round(largest / total, 6) if total > 0 else None
    return {
        "total_pnl_cents": total,
        "largest_winner_pnl_cents": round(largest, 4),
        "largest_winner_client_order_id": rows[idx].get("client_order_id"),
        "largest_winner_ticker": rows[idx].get("ticker"),
        "pnl_ex_largest_winner_cents": round(total - largest, 4),
        "largest_winner_share_of_total_pnl": share,
        "concentration_flag": bool(share is not None and share > max_winner_share),
        "concentration_threshold": max_winner_share,
    }


def _brier(records: list[dict[str, Any]]) -> dict[str, Any]:
    model_rows = []
    market_rows = []
    for row in records:
        outcome = _outcome(row.get("outcome"))
        model = _prob(row.get("entry_model_prob"))
        market = _prob(row.get("entry_market_prob"))
        if outcome is None or model is None or market is None:
            continue
        model_rows.append({"prior_p": model, "outcome": outcome})
        market_rows.append({"prior_p": market, "outcome": outcome})
    model_brier = calibration.brier(model_rows)
    market_brier = calibration.brier(market_rows)
    model_value = round(model_brier, 6) if model_rows else None
    market_value = round(market_brier, 6) if market_rows else None
    improvement = (
        round(market_value - model_value, 6)
        if model_value is not None and market_value is not None
        else None
    )
    return {
        "brier_count": len(model_rows),
        "entry_model_brier": model_value,
        "market_baseline_brier": market_value,
        "brier_improvement_vs_market": improvement,
    }


def _distance(row: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    min_n = int(thresholds["min_validated_n"])
    min_clv = float(thresholds["min_clv_beat_rate"])
    min_brier = float(thresholds["min_brier_improvement"])
    settled = int(row["settled_count"])
    clv_n = int(row["clv_measured_count"])
    brier_n = int(row["brier_count"])
    items = []
    if settled < min_n:
        items.append(f"needs {min_n - settled} more settled")
    if clv_n <= 0:
        items.append("CLV beat rate unmeasured")
    elif clv_n < min_n:
        items.append(f"needs {min_n - clv_n} more CLV-measured")
    rate = row.get("clv_beat_rate")
    if rate is not None and rate < min_clv:
        needed_hits = int(min_n * min_clv + 0.999999)
        have_hits = int(row.get("clv_beat_count") or 0)
        items.append(f"needs {max(needed_hits - have_hits, 0)} more CLV beats at n={min_n}")
    if brier_n < min_n:
        items.append(f"needs {min_n - brier_n} more Brier-comparable")
    improvement = row.get("brier_improvement_vs_market")
    if improvement is None:
        items.append("Brier vs baseline unmeasured")
    elif improvement < min_brier:
        items.append(f"needs +{round(min_brier - improvement, 6)} more Brier improvement")
    if row.get("concentration_flag"):
        items.append("track record concentrated in one winner")
    return items or ["eligible by measured validation thresholds"]


def report(
    records: Iterable[dict[str, Any]],
    *,
    default_sport: str | None = None,
    qual_rag_stats: dict[str, Any] | None = None,
    min_validated_n: int = VALIDATION_MIN_SETTLED,
    min_clv_beat_rate: float = VALIDATION_MIN_CLV_BEAT_RATE,
    min_brier_improvement: float = BRIER_MIN_IMPROVEMENT,
    concentration_max_winner_share: float = 0.50,
) -> dict[str, Any]:
    rows = [dict(row) for row in records]
    thresholds = {
        "min_validated_n": min_validated_n,
        "min_clv_beat_rate": min_clv_beat_rate,
        "min_brier_improvement": min_brier_improvement,
        "concentration_max_winner_share": concentration_max_winner_share,
    }
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        family = market_family(row, default_sport=default_sport)
        source = str(row.get("signal_source") or "consensus")
        buckets.setdefault((family, source), []).append(row)

    groups = []
    for (family, source), group_rows in sorted(buckets.items()):
        settled = len(group_rows)
        wins = sum(1 for row in group_rows if _outcome(row.get("outcome")) == 1)
        pnl = sum(_float(row.get("pnl_cents")) or 0.0 for row in group_rows)
        clv_rows = [row for row in group_rows if _float(row.get("clv_cents")) is not None]
        clv_beats = sum(1 for row in clv_rows if bool(row.get("beat_closing_consensus")))
        brier = _brier(group_rows)
        concentration = concentration_diagnostics(
            group_rows,
            max_winner_share=concentration_max_winner_share,
        )
        row = {
            "market_family": family,
            "signal_source": source,
            "settled_count": settled,
            "win_count": wins,
            "win_rate": round(wins / settled, 4) if settled else None,
            "total_pnl_cents": round(pnl, 4),
            "avg_pnl_cents": round(pnl / settled, 4) if settled else None,
            "clv_measured_count": len(clv_rows),
            "clv_unmeasured_count": settled - len(clv_rows),
            "clv_beat_count": clv_beats,
            "clv_beat_rate": round(clv_beats / len(clv_rows), 4) if clv_rows else None,
            "avg_clv_cents": (
                round(sum(float(row["clv_cents"]) for row in clv_rows) / len(clv_rows), 4)
                if clv_rows else None
            ),
            **brier,
            **concentration,
        }
        row["remaining_distance"] = _distance(row, thresholds)
        row["live_eligible"] = row["remaining_distance"] == ["eligible by measured validation thresholds"]
        groups.append(row)

    overall_concentration = concentration_diagnostics(
        rows,
        max_winner_share=concentration_max_winner_share,
    )
    return {
        "version": 1,
        "settled_count": len(rows),
        "group_count": len(groups),
        "validation_thresholds": thresholds,
        "overall_concentration": overall_concentration,
        "qual_rag": qual_rag_stats or {},
        "groups": groups,
        "notes": [
            "CLV-unmeasured settlements are excluded from CLV rates and do not satisfy the CLV criterion.",
            "Live eligibility thresholds are unchanged.",
        ],
    }
