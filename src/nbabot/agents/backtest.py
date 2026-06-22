"""Phase: backtest. Replay local learning log into research metrics."""
from __future__ import annotations

from ..alerts import deliver
from ..backtesting import format_backtest, run_backtest
from ..research import ResearchStore
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    metrics = run_backtest(
        ctx.settings.game_id,
        ctx.settings.data_path("log.jsonl"),
        ctx.scenarios,
        store,
    )
    ctx.write_json("backtest.json", metrics)
    deliver(format_backtest(metrics), ctx.settings.deliver_to)
    return metrics
