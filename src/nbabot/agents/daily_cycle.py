"""Phase: daily-cycle. Autonomous research/execution loop for one configured event."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..audit import AuditTrail
from ..research import ResearchStore
from .activation import activation_edges, run_activated_step, skip_activated_step
from .base import Context, load_context


def _execution_step(ctx: Context):
    from . import demo_execute, live_execute

    if ctx.settings.execution_mode == "live":
        return "live-execute", live_execute.run
    if ctx.settings.execution_mode == "demo":
        return "demo-execute", demo_execute.run
    return None


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    audit = AuditTrail(ctx.settings.data_dir, store)
    steps: list[dict[str, Any]] = []

    from . import backtest, book_watch, candidate_ranker, discover_markets, market_matcher, portfolio_sync, research_agent
    from . import slate_discovery, slate_verify, snapshot_market
    from . import source_check, status

    source_report = run_activated_step(
        ctx,
        "source-check",
        source_check.run,
        steps,
        audit,
        activated_by=None,
        activates=("slate-discovery",),
        dlq_type="DAILY_CYCLE_STEP",
    )
    if source_report is None:
        skip_activated_step(
            "slate-discovery",
            steps,
            activated_by="source-check",
            reason="source-check failed",
        )
        final_status = run_activated_step(
            ctx,
            "status",
            status.run,
            steps,
            audit,
            activated_by="source-check",
            dlq_type="DAILY_CYCLE_STEP",
        )
        payload = {
            "game_id": ctx.settings.game_id,
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "execution_mode": ctx.settings.execution_mode,
            "dry_run": ctx.settings.dry_run,
            "steps": steps,
            "activation_edges": activation_edges(steps),
            "status": final_status,
        }
        ctx.write_json("daily_cycle.json", payload)
        deliver(
            guardrails.with_footer(
                f"[daily-cycle] {ctx.settings.game_id}: source-check failed; "
                f"mode={ctx.settings.execution_mode} dry_run={ctx.settings.dry_run}"
            ),
            ctx.settings.deliver_to,
        )
        return payload

    slate = run_activated_step(
        ctx,
        "slate-discovery",
        slate_discovery.run,
        steps,
        audit,
        activated_by="source-check",
        activates=("slate-verify",),
        dlq_type="DAILY_CYCLE_STEP",
    )
    if slate is not None:
        run_activated_step(
            ctx,
            "slate-verify",
            slate_verify.run,
            steps,
            audit,
            activated_by="slate-discovery",
            activates=("portfolio-sync",),
            dlq_type="DAILY_CYCLE_STEP",
        )
    else:
        skip_activated_step(
            "slate-verify",
            steps,
            activated_by="slate-discovery",
            reason="slate-discovery failed",
            activates=("portfolio-sync",),
        )

    _portfolio = run_activated_step(
        ctx,
        "portfolio-sync",
        portfolio_sync.run,
        steps,
        audit,
        activated_by="slate-verify",
        activates=("discover-markets",),
        dlq_type="DAILY_CYCLE_STEP",
    )
    _catalog = run_activated_step(
        ctx,
        "discover-markets",
        discover_markets.run,
        steps,
        audit,
        activated_by="portfolio-sync",
        activates=("snapshot-market",),
        dlq_type="DAILY_CYCLE_STEP",
    )
    snapshot = run_activated_step(
        ctx,
        "snapshot-market",
        snapshot_market.run,
        steps,
        audit,
        activated_by="discover-markets",
        activates=("book-watch",),
        dlq_type="DAILY_CYCLE_STEP",
    )
    book = None
    if snapshot is not None:
        book = run_activated_step(
            ctx,
            "book-watch",
            book_watch.run,
            steps,
            audit,
            activated_by="snapshot-market",
            activates=("market-matcher",),
            dlq_type="DAILY_CYCLE_STEP",
        )
    else:
        skip_activated_step(
            "book-watch",
            steps,
            activated_by="snapshot-market",
            reason="snapshot-market failed",
            activates=("market-matcher",),
        )

    exec_step = _execution_step(ctx)
    if snapshot is None:
        skip_activated_step(
            "market-matcher",
            steps,
            activated_by="book-watch",
            reason="snapshot-market failed",
            activates=("candidate-ranker",),
        )
        skip_activated_step(
            "candidate-ranker",
            steps,
            activated_by="market-matcher",
            reason="snapshot-market failed",
            activates=("research-agent",),
        )
        skip_activated_step(
            "research-agent",
            steps,
            activated_by="candidate-ranker",
            reason="snapshot-market failed",
            activates=("execution",),
        )
        skip_activated_step(
            "execution",
            steps,
            activated_by="research-agent",
            reason="snapshot-market failed",
            activates=("backtest",),
        )
    elif book is None:
        skip_activated_step(
            "market-matcher",
            steps,
            activated_by="book-watch",
            reason="book-watch failed",
            activates=("candidate-ranker",),
        )
        skip_activated_step(
            "candidate-ranker",
            steps,
            activated_by="market-matcher",
            reason="book-watch failed",
            activates=("research-agent",),
        )
        skip_activated_step(
            "research-agent",
            steps,
            activated_by="candidate-ranker",
            reason="book-watch failed",
            activates=("execution",),
        )
        skip_activated_step(
            "execution",
            steps,
            activated_by="research-agent",
            reason="book-watch failed",
            activates=("backtest",),
        )
    else:
        matches = run_activated_step(
            ctx,
            "market-matcher",
            market_matcher.run,
            steps,
            audit,
            activated_by="book-watch",
            activates=("candidate-ranker",),
            dlq_type="DAILY_CYCLE_STEP",
        )
        if matches is None:
            skip_activated_step(
                "candidate-ranker",
                steps,
                activated_by="market-matcher",
                reason="market-matcher failed",
                activates=("research-agent",),
            )
            skip_activated_step(
                "research-agent",
                steps,
                activated_by="candidate-ranker",
                reason="market-matcher failed",
                activates=("execution",),
            )
            skip_activated_step(
                "execution",
                steps,
                activated_by="research-agent",
                reason="market-matcher failed",
                activates=("backtest",),
            )
        else:
            ranked = run_activated_step(
                ctx,
                "candidate-ranker",
                candidate_ranker.run,
                steps,
                audit,
                activated_by="market-matcher",
                activates=("research-agent",),
                dlq_type="DAILY_CYCLE_STEP",
            )
            if ranked is None:
                skip_activated_step(
                    "research-agent",
                    steps,
                    activated_by="candidate-ranker",
                    reason="candidate-ranker failed",
                    activates=("execution",),
                )
                skip_activated_step(
                    "execution",
                    steps,
                    activated_by="research-agent",
                    reason="candidate-ranker failed",
                    activates=("backtest",),
                )
            else:
                research = run_activated_step(
                    ctx,
                    "research-agent",
                    research_agent.run,
                    steps,
                    audit,
                    activated_by="candidate-ranker",
                    activates=("execution",),
                    dlq_type="DAILY_CYCLE_STEP",
                )
                if research is None:
                    skip_activated_step(
                        "execution",
                        steps,
                        activated_by="research-agent",
                        reason="research-agent failed",
                        activates=("backtest",),
                    )
                elif exec_step is None:
                    skip_activated_step(
                        "execution",
                        steps,
                        activated_by="research-agent",
                        reason="daily-cycle does not paper trade; set live/demo mode explicitly",
                        activates=("backtest",),
                    )
                else:
                    exec_name, exec_fn = exec_step
                    run_activated_step(
                        ctx,
                        exec_name,
                        exec_fn,
                        steps,
                        audit,
                        activated_by="research-agent",
                        activates=("backtest",),
                        dlq_type="DAILY_CYCLE_STEP",
                    )

    if ctx.settings.data_path("log.jsonl").exists():
        run_activated_step(
            ctx,
            "backtest",
            backtest.run,
            steps,
            audit,
            activated_by="execution",
            activates=("status",),
            dlq_type="DAILY_CYCLE_STEP",
        )
    else:
        skip_activated_step(
            "backtest",
            steps,
            activated_by="execution",
            reason="no learning log",
            activates=("status",),
        )

    final_status = run_activated_step(
        ctx,
        "status",
        status.run,
        steps,
        audit,
        activated_by="backtest",
        dlq_type="DAILY_CYCLE_STEP",
    )
    payload = {
        "game_id": ctx.settings.game_id,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "execution_mode": ctx.settings.execution_mode,
        "dry_run": ctx.settings.dry_run,
        "steps": steps,
        "activation_edges": activation_edges(steps),
        "status": final_status,
    }
    ctx.write_json("daily_cycle.json", payload)
    failures = sum(1 for step in steps if step.get("status") == "error")
    skipped = sum(1 for step in steps if step.get("status") == "skipped")
    deliver(
        guardrails.with_footer(
            f"[daily-cycle] {ctx.settings.game_id}: steps={len(steps)} "
            f"failures={failures} skipped={skipped} mode={ctx.settings.execution_mode} "
            f"dry_run={ctx.settings.dry_run}"
        ),
        ctx.settings.deliver_to,
    )
    return payload
