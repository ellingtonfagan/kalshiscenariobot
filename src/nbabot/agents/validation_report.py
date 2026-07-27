"""Phase: validation-report. Show live-eligibility evidence and gaps."""
from __future__ import annotations

from typing import Any

from .. import guardrails
from ..alerts import deliver
from ..research import ResearchStore, utc_now
from ..validation_report import report as build_validation_report
from .base import Context, load_context


def compact_lines(payload: dict[str, Any], *, limit: int = 6) -> list[str]:
    lines = [
        f"[validation-report] settled={payload.get('settled_count', 0)} groups={payload.get('group_count', 0)}",
    ]
    concentration = payload.get("overall_concentration") or {}
    share = concentration.get("largest_winner_share_of_total_pnl")
    share_text = "n/a" if share is None else f"{float(share) * 100:.1f}%"
    lines.append(
        "concentration "
        f"pnl_ex_largest=${float(concentration.get('pnl_ex_largest_winner_cents') or 0) / 100:.2f} "
        f"largest_share={share_text} "
        f"flag={bool(concentration.get('concentration_flag'))}"
    )
    for row in (payload.get("groups") or [])[:limit]:
        rate = row.get("clv_beat_rate")
        rate_text = "unmeasured" if rate is None else f"{float(rate) * 100:.1f}%"
        win_rate = row.get("win_rate")
        win_text = "n/a" if win_rate is None else f"{float(win_rate) * 100:.1f}%"
        distance = "; ".join(str(item) for item in (row.get("remaining_distance") or []))
        lines.append(
            f"{row.get('market_family')} | {row.get('signal_source')}: "
            f"n={row.get('settled_count', 0)} wins={row.get('win_count', 0)} "
            f"win_rate={win_text} pnl=${float(row.get('total_pnl_cents') or 0) / 100:.2f} "
            f"clv={row.get('clv_measured_count', 0)} beat={rate_text} "
            f"avg_clv={row.get('avg_clv_cents')} "
            f"distance={distance}"
        )
    return lines


def _format(payload: dict[str, Any]) -> str:
    return "\n".join(compact_lines(payload))


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    payload = build_validation_report(
        store.list_settlement_records(),
        default_sport=ctx.settings.sport,
        concentration_max_winner_share=float(
            getattr(ctx.settings, "concentration_max_winner_share", 0.50)
        ),
    )
    payload["game_id"] = ctx.settings.game_id
    payload["generated_at"] = utc_now()
    ctx.write_json("validation_report.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
