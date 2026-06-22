"""Local scenario backtesting from the append-only learning log."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

from . import calibration
from .research import ResearchStore, utc_now
from .scenarios import Scenario


@dataclass(frozen=True)
class BacktestRow:
    game_id: str
    scenario_id: str
    prior_p_joint: float
    market_p_joint: float | None
    edge: float | None
    hit: int
    resolved_legs: int
    total_legs: int
    simulated_pnl: float


def _brier(rows: list[BacktestRow]) -> float:
    if not rows:
        return float("nan")
    return sum((r.prior_p_joint - r.hit) ** 2 for r in rows) / len(rows)


def _drawdown(pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _leg_key(row: dict) -> tuple[str, float, float]:
    return (row["market"], round(float(row["line"]), 4), round(float(row["prior_p"]), 6))


def _row_from_entry(entry: dict, scenario: Scenario) -> BacktestRow | None:
    exact = {_leg_key(row): row for row in entry.get("legs", [])}
    joint = 1.0
    market_joint = 1.0
    market_count = 0
    resolved = 0
    hits = 0

    for leg in scenario.legs:
        if not leg.resolvable:
            continue
        row = exact.get((leg.market, round(leg.line, 4), round(leg.prior_p, 6)))
        if row is None or row.get("outcome") is None:
            continue
        joint *= float(row.get("prior_p", leg.prior_p))
        resolved += 1
        hits += int(row["outcome"] == 1)
        if row.get("entry_implied_p") is not None:
            market_joint *= float(row["entry_implied_p"])
            market_count += 1

    if resolved == 0:
        return None

    hit = int(hits == resolved)
    market_p = market_joint if market_count == resolved else None
    edge = (joint - market_p) if market_p is not None else None
    simulated_pnl = 0.0
    if market_p is not None and edge is not None and abs(edge) > 0:
        simulated_pnl = (1.0 - market_p) if hit else -market_p

    return BacktestRow(
        game_id=entry["game_id"],
        scenario_id=scenario.id,
        prior_p_joint=round(joint, 5),
        market_p_joint=round(market_p, 5) if market_p is not None else None,
        edge=round(edge, 5) if edge is not None else None,
        hit=hit,
        resolved_legs=resolved,
        total_legs=len(scenario.legs),
        simulated_pnl=round(simulated_pnl, 5),
    )


def run_backtest(game_id: str, log_path: Path, scenarios: list[Scenario],
                 store: ResearchStore | None = None) -> dict:
    entries = calibration.load_log(log_path)
    rows: list[BacktestRow] = []
    for entry in entries:
        for scenario in scenarios:
            row = _row_from_entry(entry, scenario)
            if row:
                rows.append(row)

    pnls = [r.simulated_pnl for r in rows]
    total_capital = sum(1 for r in rows if r.market_p_joint is not None)
    total_pnl = sum(pnls)
    metrics = {
        "run_id": f"{game_id}-{utc_now()}",
        "game_id": game_id,
        "scenario_count": len(rows),
        "hit_rate": round(sum(r.hit for r in rows) / len(rows), 4) if rows else 0.0,
        "brier_prior": round(_brier(rows), 4) if rows else math.nan,
        "edge_signals": sum(1 for r in rows if r.edge is not None),
        "flat_bet_pnl_units": round(total_pnl, 4),
        "flat_bet_roi": round(total_pnl / total_capital, 4) if total_capital else 0.0,
        "max_drawdown_units": round(_drawdown(pnls), 4),
        "calibration": calibration.recompute(log_path),
        "scenario_rows": [asdict(r) for r in rows],
    }
    if store:
        store.record_backtest(metrics["run_id"], game_id, metrics)
    return metrics


def format_backtest(metrics: dict) -> str:
    return (
        f"[backtest] {metrics['game_id']} scenarios={metrics['scenario_count']} "
        f"hit_rate={metrics['hit_rate']:.3f} brier={metrics['brier_prior']:.4f} "
        f"edge_signals={metrics['edge_signals']} "
        f"pnl={metrics['flat_bet_pnl_units']:+.3f}u "
        f"max_dd={metrics['max_drawdown_units']:.3f}u"
    )
