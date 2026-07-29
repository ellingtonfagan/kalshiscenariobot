"""Phase: source-check. Report external source readiness without leaking secrets."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..sources import build_source_report, format_source_report
from .base import Context, load_context


def _kalshi_scope_probe(ctx: Context, network_enabled: bool) -> dict:
    if not network_enabled:
        return {"attempted": False, "reason": "network-disabled"}
    try:
        from ..slate import _classify_kalshi_sports, _kalshi_quote_fields, configured_sports
        from ..sources.registry import load_source_config

        config = load_source_config()
        sports = set(configured_sports(ctx.settings, config))
        raw = ctx.kalshi.list_open_markets(max_pages=1)
        counts: dict[str, int] = {}
        nonzero = 0
        in_scope = 0
        for market in raw:
            title = market.get("title") or ""
            matched = [
                sport for sport in _classify_kalshi_sports(title, config)
                if sport in sports or sport == "kalshi_sports"
            ]
            if not matched:
                continue
            in_scope += 1
            if _kalshi_quote_fields(market).get("has_live_quote"):
                nonzero += 1
            for sport in matched:
                counts[sport] = counts.get(sport, 0) + 1
        return {
            "attempted": True,
            "ok": True,
            "open_markets_scanned": len(raw),
            "in_scope_markets": in_scope,
            "nonzero_quote_markets": nonzero,
            "sports": dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)),
        }
    except Exception as exc:
        return {"attempted": True, "ok": False, "reason": exc.__class__.__name__}


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    report = build_source_report()
    report["kalshi_scope_probe"] = _kalshi_scope_probe(ctx, bool(report.get("network_enabled")))
    ctx.write_json("source_check.json", report)
    deliver(guardrails.with_footer(format_source_report(report)), ctx.settings.deliver_to)
    return report
