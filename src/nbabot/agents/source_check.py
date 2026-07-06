"""Phase: source-check. Report external source readiness without leaking secrets."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..sources import build_source_report, format_source_report
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    report = build_source_report()
    ctx.write_json("source_check.json", report)
    deliver(guardrails.with_footer(format_source_report(report)), ctx.settings.deliver_to)
    return report
