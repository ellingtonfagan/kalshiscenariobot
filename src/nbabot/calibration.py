"""§6 learning loop: the JSONL log + Brier score + correlation haircut.

This is the only part of the system that makes the bot *improve*. reconcile writes one
LogEntry per game; recompute() reads the whole log and returns updated calibration.
Reconcile writes conservative override suggestions that load_context applies in memory
without rewriting config/*.scenarios.yaml.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR
from .scenarios import Scenario, load_scenarios


@dataclass
class LegResult:
    market: str
    line: float
    prior_p: float
    entry_implied_p: float | None
    outcome: int | None        # 1/0, or None if unresolvable


@dataclass
class ScenarioResult:
    id: str
    prior_p_joint: float | None
    hit: int                   # 1 if all resolvable legs hit
    notes: str = ""
    resolved_legs: int | None = None
    total_legs: int | None = None


@dataclass
class LogEntry:
    game_id: str
    logged_at: str
    legs: list[LegResult] = field(default_factory=list)
    scenarios: list[ScenarioResult] = field(default_factory=list)


def append_log(path: Path, entry: LogEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def brier(rows: list[dict]) -> float:
    """rows: [{'prior_p':float,'outcome':0|1}]. 0.25 = coin-flip baseline. Lower better."""
    rows = [r for r in rows if r.get("outcome") is not None]
    if not rows:
        return float("nan")
    return sum((r["prior_p"] - r["outcome"]) ** 2 for r in rows) / len(rows)


def _family(market: str) -> str:
    # group by stat suffix: brunson_points -> points, towns_rebounds -> rebounds
    return market.rsplit("_", 1)[-1]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _scenario_config(game_id: str | None, cache: dict[str, dict[str, Scenario] | None]
                     ) -> dict[str, Scenario] | None:
    if not game_id:
        return None
    if game_id in cache:
        return cache[game_id]
    path = CONFIG_DIR / f"{game_id}.scenarios.yaml"
    if not path.exists():
        cache[game_id] = None
        return None
    import yaml

    scen, _, _ = load_scenarios(yaml.safe_load(path.read_text()))
    cache[game_id] = {s.id: s for s in scen}
    return cache[game_id]


def _resolvable_markets(scenarios: dict[str, Scenario] | None) -> dict[str, bool]:
    if not scenarios:
        return {}
    out: dict[str, bool] = {}
    for scenario in scenarios.values():
        for leg in scenario.legs:
            out[leg.market] = bool(leg.resolvable)
    return out


def _leg_key(row: dict) -> tuple[str, float, float]:
    return (row["market"], round(float(row["line"]), 4), round(float(row["prior_p"]), 6))


def _repair_legacy_scenario(entry: dict, scenario: Scenario) -> dict | None:
    """Rebuild old scenario rows whose joint prior included unresolved legs."""
    exact: dict[tuple[str, float, float], dict] = {}
    by_market_line: dict[tuple[str, float], list[dict]] = {}
    for row in entry.get("legs", []):
        exact[_leg_key(row)] = row
        by_market_line.setdefault((row["market"], round(float(row["line"]), 4)), []).append(row)

    joint = 1.0
    resolved = 0
    hits = 0
    for leg in scenario.legs:
        if not leg.resolvable:
            continue

        row = exact.get((leg.market, round(float(leg.line), 4), round(float(leg.prior_p), 6)))
        if row is None:
            candidates = by_market_line.get((leg.market, round(float(leg.line), 4)), [])
            row = candidates[0] if len(candidates) == 1 else None
        if row is None or row.get("outcome") is None:
            continue

        joint *= float(row.get("prior_p", leg.prior_p))
        resolved += 1
        hits += int(row["outcome"] == 1)

    if resolved == 0:
        return None
    return {
        "id": scenario.id,
        "prior_p_joint": round(joint, 5),
        "hit": int(hits == resolved),
        "resolved_legs": resolved,
        "total_legs": len(scenario.legs),
    }


def _scenario_row(entry: dict, row: dict, scenarios: dict[str, Scenario] | None) -> dict | None:
    if row.get("resolved_legs") is None and scenarios and row.get("id") in scenarios:
        return _repair_legacy_scenario(entry, scenarios[row["id"]])
    if row.get("resolved_legs") == 0:
        return None
    if row.get("prior_p_joint") is None or row.get("hit") is None:
        return None
    return row


def recompute(path: Path) -> dict:
    """Return calibration summary across the whole log."""
    log = load_log(path)
    fam_rows: dict[str, list[dict]] = {}
    scen_rows: dict[str, list[dict]] = {}
    scen_joint: dict[str, list[tuple[float, int]]] = {}
    scenario_cache: dict[str, dict[str, Scenario] | None] = {}

    for entry in log:
        scenarios = _scenario_config(entry.get("game_id"), scenario_cache)
        resolvable_markets = _resolvable_markets(scenarios)
        for leg in entry.get("legs", []):
            if leg.get("outcome") is None:
                continue
            if resolvable_markets and not resolvable_markets.get(leg["market"], True):
                continue
            fam_rows.setdefault(_family(leg["market"]), []).append(
                {"prior_p": leg["prior_p"], "outcome": leg["outcome"]})
        for sc in entry.get("scenarios", []):
            row = _scenario_row(entry, sc, scenarios)
            if row is None:
                continue
            prior = float(row["prior_p_joint"])
            hit = int(row["hit"])
            scen_rows.setdefault(row["id"], []).append(
                {"prior_p": prior, "outcome": hit})
            scen_joint.setdefault(row["id"], []).append((prior, hit))

    summary = {"families": {}, "scenarios": {}, "haircut": {}}
    for fam, rows in fam_rows.items():
        bias = sum(r["prior_p"] - r["outcome"] for r in rows) / len(rows)
        summary["families"][fam] = {
            "brier": round(brier(rows), 4), "n": len(rows), "bias": round(bias, 4),
            "too_high": bool(bias > 0.08 and len(rows) >= 15),
            "too_low": bool(bias < -0.08 and len(rows) >= 15),
        }
    for sid, rows in scen_rows.items():
        summary["scenarios"][sid] = {"brier": round(brier(rows), 4), "n": len(rows)}
    # correlation haircut = realized joint hit rate / mean predicted joint prob
    for sid, pairs in scen_joint.items():
        pred = sum(p for p, _ in pairs) / len(pairs)
        realized = sum(h for _, h in pairs) / len(pairs)
        summary["haircut"][sid] = round(realized / pred, 3) if pred > 0 else 1.0

    return summary


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def apply_overrides(scenarios: list[Scenario], haircut: dict[str, float],
                    overrides: dict[str, Any]) -> tuple[list[Scenario], dict[str, float]]:
    """Apply conservative calibration overrides without mutating config YAML data."""
    if not overrides:
        return scenarios, haircut

    out_scenarios = deepcopy(scenarios)
    out_haircut = dict(haircut)

    for sid, raw in (overrides.get("sgp_haircut") or {}).items():
        try:
            # Do not automatically make scenario probabilities more aggressive.
            out_haircut[str(sid)] = round(_clamp(float(raw), 0.05, 1.0), 5)
        except (TypeError, ValueError):
            continue

    family_mult = overrides.get("family_prior_multipliers") or {}
    market_mult = overrides.get("market_prior_multipliers") or {}
    for scenario in out_scenarios:
        for leg in scenario.legs:
            mults = []
            if _family(leg.market) in family_mult:
                mults.append(family_mult[_family(leg.market)])
            if leg.market in market_mult:
                mults.append(market_mult[leg.market])
            for raw in mults:
                try:
                    # Automatic calibration may shrink priors, not raise them.
                    leg.prior_p = round(_clamp(leg.prior_p * _clamp(float(raw), 0.01, 1.0), 0.01, 0.99), 5)
                except (TypeError, ValueError):
                    continue

    return out_scenarios, out_haircut


def suggest_overrides(summary: dict[str, Any], game_id: str,
                      min_family_n: int = 15, min_scenario_n: int = 5,
                      max_prior_shift: float = 0.05) -> dict[str, Any]:
    """Create conservative override suggestions from a recompute() summary."""
    overrides: dict[str, Any] = {
        "version": 1,
        "source": "calibration.recompute",
        "source_game_id": game_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_family_n": min_family_n,
        "min_scenario_n": min_scenario_n,
        "family_prior_multipliers": {},
        "market_prior_multipliers": {},
        "sgp_haircut": {},
        "notes": [],
    }

    for family, row in (summary.get("families") or {}).items():
        n = int(row.get("n", 0) or 0)
        bias = float(row.get("bias", 0.0) or 0.0)
        if n >= min_family_n and bias > 0.08:
            shift = _clamp(abs(bias), 0.0, max_prior_shift)
            overrides["family_prior_multipliers"][family] = round(1.0 - shift, 5)
            overrides["notes"].append(
                f"{family}: priors ran {bias:+.3f} high over n={n}; shrink by {shift:.3f}"
            )

    scenario_rows = summary.get("scenarios") or {}
    for sid, raw_haircut in (summary.get("haircut") or {}).items():
        n = int((scenario_rows.get(sid) or {}).get("n", 0) or 0)
        try:
            value = float(raw_haircut)
        except (TypeError, ValueError):
            continue
        if n >= min_scenario_n and value < 1.0:
            overrides["sgp_haircut"][sid] = round(_clamp(value, 0.05, 1.0), 5)
            overrides["notes"].append(
                f"{sid}: learned SGP haircut {value:.3f} over n={n}"
            )

    return overrides
