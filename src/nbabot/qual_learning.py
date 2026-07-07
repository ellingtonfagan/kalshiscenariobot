"""Pure helpers for qualitative engine lessons and calibration."""
from __future__ import annotations

import math
import re
from typing import Any, Iterable


DEFAULT_CONFIDENCE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.55, 0.65),
    (0.65, 0.75),
    (0.75, 0.85),
    (0.85, 1.01),
)


def normalize_lesson(text: Any) -> str:
    """Return a stable dedup key for a lesson sentence."""
    raw = str(text or "").strip().lower()
    raw = re.sub(r"https?://\S+", "", raw)
    raw = re.sub(r"[^a-z0-9 ]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _prob(raw: Any) -> float | None:
    try:
        if raw is None or raw == "":
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 1:
        return value
    return None


def _outcome(raw: Any) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value in {0, 1} else None


def confidence_bucket(
    confidence: Any,
    buckets: tuple[tuple[float, float], ...] = DEFAULT_CONFIDENCE_BUCKETS,
) -> tuple[float, float] | None:
    value = _prob(confidence)
    if value is None:
        return None
    for lo, hi in buckets:
        upper_ok = value < hi if hi < 1.01 else value <= 1.0
        if lo <= value and upper_ok:
            return lo, min(hi, 1.0)
    return None


def qual_calibration_stats(
    rows: Iterable[dict[str, Any]],
    *,
    buckets: tuple[tuple[float, float], ...] = DEFAULT_CONFIDENCE_BUCKETS,
) -> list[dict[str, Any]]:
    """Bucket settled qualitative signals by analyst confidence.

    ``prob`` is the model probability for the settled side; ``outcome`` is the
    settled side outcome. A signal is counted correct when the probability was
    on the right side of 0.50.
    """
    grouped: dict[tuple[float, float], list[tuple[float, int]]] = {bucket: [] for bucket in buckets}
    for row in rows:
        bucket = confidence_bucket(row.get("confidence"), buckets)
        prob = _prob(row.get("prob", row.get("entry_model_prob")))
        outcome = _outcome(row.get("outcome"))
        if bucket is None or prob is None or outcome is None:
            continue
        grouped.setdefault(bucket, []).append((prob, outcome))

    out = []
    for lo, hi in buckets:
        values = grouped.get((lo, hi), [])
        n = len(values)
        correct = sum(1 for prob, outcome in values if (prob >= 0.5) == bool(outcome))
        wins = sum(outcome for _, outcome in values)
        mean_prob = sum(prob for prob, _ in values) / n if n else None
        observed_rate = wins / n if n else None
        brier = sum((prob - outcome) ** 2 for prob, outcome in values) / n if n else None
        out.append({
            "bucket": f"{lo:.2f}-{hi:.2f}",
            "lo": lo,
            "hi": hi,
            "count": n,
            "correct": correct,
            "correct_rate": round(correct / n, 6) if n else None,
            "observed_rate": round(observed_rate, 6) if observed_rate is not None else None,
            "mean_prob": round(mean_prob, 6) if mean_prob is not None else None,
            "brier": round(brier, 6) if brier is not None and math.isfinite(brier) else None,
        })
    return out


def format_calibration_lines(stats: Iterable[dict[str, Any]]) -> list[str]:
    lines = []
    for row in stats:
        n = int(row.get("count") or 0)
        if n <= 0:
            lines.append(f"your {row.get('bucket')} confidence signals have no settled sample yet")
            continue
        correct = int(row.get("correct") or 0)
        pct = int(round(float(row.get("correct_rate") or 0.0) * 100))
        brier = row.get("brier")
        brier_text = "n/a" if brier is None else f"{float(brier):.3f}"
        lines.append(
            f"your {row.get('bucket')} confidence signals have resolved correctly "
            f"{correct}/{n} times ({pct}%), Brier {brier_text}"
        )
    return lines
