"""Small dependency-free local web UI for broad-slate edge artifacts."""
from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .edge_engine import IMPLAUSIBLE_EDGE_BLOCKER
from .performance_learner import VALIDATION_MIN_CLV_BEAT_RATE, VALIDATION_MIN_SETTLED
from .research import ResearchStore

if TYPE_CHECKING:
    from .agents.base import Context

LIVE_ACK = "LIVE_TRADES_REAL_MONEY"
BROAD_SLATE_ACK = "BROAD_SLATE_TRADES_REAL_MONEY"
STRUCTURED_PROVIDERS = ("parlay_api", "sportsgameodds", "the_odds_api")


def _json_default(obj: Any) -> str:
    return str(obj)


def _latest_artifact_path(ctx: Context, suffix: str) -> Path | None:
    data_dir = Path(ctx.settings.data_dir)
    matches = [p for p in data_dir.glob(f"*.{suffix}") if p.is_file()] if data_dir.exists() else []
    if matches:
        return max(matches, key=lambda p: (p.stat().st_mtime_ns, p.name))
    fallback = ctx.settings.data_path(suffix)
    return fallback if fallback.exists() else None


def _read_artifact(ctx: Context, suffix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    path = _latest_artifact_path(ctx, suffix)
    meta: dict[str, Any] = {"suffix": suffix, "path": str(path) if path else "", "present": bool(path)}
    if path is None:
        meta["status"] = "missing"
        return {}, meta
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        meta["status"] = f"unreadable: {exc.__class__.__name__}"
        return {}, meta
    meta["status"] = "ok"
    return payload if isinstance(payload, dict) else {}, meta


def _load_artifacts(ctx: Context) -> dict[str, Any]:
    suffixes = [
        "candidate_ranker.json",
        "research_bundle.json",
        "performance_learner.json",
        "validation_report.json",
        "slate_candidates.json",
        "source_check.json",
        "settlement_audit.json",
        "qual_postmortem.json",
    ]
    artifacts: dict[str, Any] = {"_meta": {}}
    for suffix in suffixes:
        key = suffix.removesuffix(".json")
        artifacts[key], artifacts["_meta"][key] = _read_artifact(ctx, suffix)
    return artifacts


def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    if not rows:
        return "<p class='empty'>No rows yet.</p>"
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(
            f"<td>{html.escape(str(row.get(c, ''))[:240])}</td>" for c in cols
        ) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_count(value: Any) -> str:
    return f"{int(value):,}" if isinstance(value, int) else html.escape(str(value))


def _fmt_prob(value: Any) -> str:
    val = _safe_float(value)
    return "n/a" if val is None else f"{val * 100:.1f}%"


def _fmt_edge(value: Any) -> str:
    val = _safe_float(value)
    return "n/a" if val is None else f"{val * 100:+.1f} pp"


def _fmt_cents(value: Any) -> str:
    val = _safe_float(value)
    return "n/a" if val is None else f"{val:.1f} cents"


def _join(values: Any) -> str:
    if isinstance(values, str):
        return values
    if isinstance(values, list):
        return ", ".join(str(v) for v in values if v not in (None, ""))
    return "" if values is None else str(values)


def _execution_state(ctx: Context) -> dict[str, Any]:
    settings = ctx.settings
    live_cleared = (
        settings.execution_mode == "live"
        and not settings.dry_run
        and settings.live_trading_ack == LIVE_ACK
        and getattr(settings, "broad_slate_execution", "") == BROAD_SLATE_ACK
    )
    mode = "live" if settings.execution_mode == "live" and not settings.dry_run else "monitor-only / dry-run"
    blockers = []
    if settings.execution_mode != "live":
        blockers.append("execution_mode is not live")
    if settings.dry_run:
        blockers.append("dry_run is enabled")
    if settings.live_trading_ack != LIVE_ACK:
        blockers.append("live trading ack missing")
    if getattr(settings, "broad_slate_execution", "") != BROAD_SLATE_ACK:
        blockers.append("broad-slate live ack missing")
    return {"mode": mode, "live_cleared": live_cleared, "blockers": blockers}


def _performance_learning(artifacts: dict[str, Any]) -> dict[str, Any]:
    bundle = artifacts.get("research_bundle") or {}
    learning = bundle.get("performance_learning") or bundle.get("performance_learner")
    if isinstance(learning, dict):
        return learning
    direct = artifacts.get("performance_learner") or {}
    return direct if isinstance(direct, dict) else {}


def _validated_families(learning: dict[str, Any]) -> list[dict[str, Any]]:
    families = learning.get("families") if isinstance(learning, dict) else {}
    rows: list[dict[str, Any]] = []
    if isinstance(families, dict):
        iterable = families.items()
    elif isinstance(families, list):
        iterable = ((str(row.get("family") or row.get("market_family") or ""), row) for row in families)
    else:
        iterable = []
    for family, row in iterable:
        if not isinstance(row, dict):
            continue
        if row.get("validated") or row.get("market_type_verdict") == "validated":
            rows.append({"family": family, **row})
    return rows


def _validation_report(ctx: Context, artifacts: dict[str, Any]) -> dict[str, Any]:
    direct = artifacts.get("validation_report") or {}
    if isinstance(direct, dict) and direct.get("groups") is not None:
        return direct
    try:
        from .validation_report import report as build_validation_report

        return build_validation_report(
            ResearchStore(ctx.settings.research_db_path).list_settlement_records(),
            default_sport=ctx.settings.sport,
            concentration_max_winner_share=float(
                getattr(ctx.settings, "concentration_max_winner_share", 0.50)
            ),
        )
    except Exception:
        return {"settled_count": 0, "groups": [], "overall_concentration": {}}


def _validation_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in report.get("groups") or []:
        clv_rate = row.get("clv_beat_rate")
        rows.append({
            "market_family": row.get("market_family"),
            "signal_source": row.get("signal_source"),
            "settled": row.get("settled_count", 0),
            "wins": row.get("win_count", 0),
            "pnl": f"${float(row.get('total_pnl_cents') or 0) / 100:.2f}",
            "clv_measured": row.get("clv_measured_count", 0),
            "clv_beat_rate": "unmeasured" if clv_rate is None else f"{float(clv_rate) * 100:.1f}%",
            "avg_clv": _fmt_cents(row.get("avg_clv_cents")),
            "distance": "; ".join(str(item) for item in (row.get("remaining_distance") or [])),
        })
    return rows


def _settlement_rows(ctx: Context, artifacts: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    try:
        store_rows = ResearchStore(ctx.settings.research_db_path).latest_rows("settlement_records", 8)
    except Exception:
        store_rows = []
    if store_rows:
        try:
            count = len(ResearchStore(ctx.settings.research_db_path).list_settlement_records())
        except Exception:
            count = len(store_rows)
        return store_rows, count

    audit_records = (artifacts.get("settlement_audit") or {}).get("records") or []
    rows = [row for row in audit_records if isinstance(row, dict)]
    return rows[:8], len(rows)


def _candidate_rows(ranker: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in ranker.get("rows", []) if isinstance(row, dict)]

    def sort_key(row: dict[str, Any]) -> tuple[int, float]:
        if row.get("trade_eligible"):
            group = 0
        elif row.get("passes_edge"):
            group = 1
        else:
            group = 2
        edge = _safe_float(row.get("edge"))
        return group, -(edge if edge is not None else -999.0)

    return sorted(rows, key=sort_key)


def _candidate_price(row: dict[str, Any]) -> Any:
    if row.get("kalshi_price_cents") is not None:
        return row.get("kalshi_price_cents")
    executable = row.get("executable_price") if isinstance(row.get("executable_price"), dict) else {}
    for key in ("vwap_cents", "ask_cents", "bid_cents"):
        if executable.get(key) is not None:
            return executable.get(key)
    return None


def _market_label(row: dict[str, Any]) -> str:
    identity = row.get("market_identity") if isinstance(row.get("market_identity"), dict) else {}
    pieces = [
        identity.get("league") or identity.get("sport"),
        identity.get("market_type"),
        identity.get("participant") or identity.get("side"),
    ]
    if identity.get("line") is not None:
        pieces.append(str(identity.get("line")))
    label = " ".join(str(p) for p in pieces if p)
    return label or str(row.get("market") or row.get("label") or row.get("ticker") or "")


def _display_blocker(blocker: str) -> str:
    if blocker.startswith("edge exceeds plausible maximum"):
        return "edge exceeds plausible maximum"
    return blocker


def _status_for_candidate(row: dict[str, Any]) -> tuple[str, str, str]:
    blockers = [str(item) for item in (row.get("blockers") or []) if item]
    if row.get("trade_eligible"):
        return "trade_eligible", "eligible", ""
    if any(item == IMPLAUSIBLE_EDGE_BLOCKER or item.startswith("edge exceeds plausible maximum") for item in blockers):
        return "flagged: implausible edge", "flagged", "edge exceeds plausible maximum"
    if blockers:
        return "blocked", "blocked", _display_blocker(blockers[0])
    if row.get("passes_edge"):
        return "passing", "eligible", ""
    return "blocked", "blocked", "no pass signal"


def _render_edge_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='empty'>No edge candidates yet.</p>"
    body = []
    for row in rows:
        status, status_class, reason = _status_for_candidate(row)
        consensus = row.get("consensus") if isinstance(row.get("consensus"), dict) else {}
        providers = row.get("providers") or consensus.get("providers") or row.get("structured_sources")
        book_count = row.get("book_count", consensus.get("book_count", ""))
        cells = [
            _market_label(row),
            row.get("ticker", ""),
            row.get("signal_source", "consensus"),
            _fmt_prob(row.get("model_prob")),
            _fmt_cents(_candidate_price(row)),
            _fmt_edge(row.get("edge")),
            book_count,
            _join(providers),
            status,
            reason,
        ]
        body.append(
            f"<tr class='{html.escape(status_class)}'>"
            + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells)
            + "</tr>"
        )
    head = "".join(
        f"<th>{html.escape(label)}</th>"
        for label in ["Market", "Ticker", "Signal", "Model Prob", "Kalshi Price", "Edge", "Books", "Providers", "Status", "Reason"]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _provider_status_rows(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    slate_status = artifacts.get("slate_candidates", {}).get("provider_status") or []
    source_providers = {
        row.get("key"): row
        for row in (artifacts.get("source_check", {}).get("providers") or [])
        if isinstance(row, dict)
    }
    rows = []
    for provider in STRUCTURED_PROVIDERS:
        provider_rows = [
            row for row in slate_status
            if isinstance(row, dict) and row.get("provider") == provider
        ]
        source = source_providers.get(provider) or {}
        configured = bool(source.get("configured")) or bool(provider_rows)
        active = any(bool(row.get("ok")) and int(row.get("rows") or 0) > 0 for row in provider_rows)
        probe = source.get("probe") if isinstance(source.get("probe"), dict) else {}
        if not active and probe.get("ok") and not provider_rows:
            active = True
        credit_reasons = [
            str(row.get("skip_reason") or row.get("reason") or row.get("error") or "")
            for row in provider_rows
        ]
        credit_gated = any("credit" in reason.lower() for reason in credit_reasons)
        latest = provider_rows[-1] if provider_rows else {}
        remaining = (
            latest.get("x_requests_remaining")
            or latest.get("credits_remaining")
            or latest.get("remaining_credits")
            or ""
        )
        total_rows = sum(int(row.get("rows") or 0) for row in provider_rows)
        if credit_gated:
            state = "credit-gated"
        elif active:
            state = "active"
        elif configured:
            state = "configured"
        else:
            state = "not configured"
        detail = latest.get("skip_reason") or latest.get("reason") or latest.get("error") or probe.get("reason") or ""
        rows.append({
            "provider": provider,
            "configured": "yes" if configured else "no",
            "state": state,
            "rows": total_rows if provider_rows else "",
            "remaining_credits": remaining,
            "detail": detail,
        })
    return rows


def _render_settlements(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='empty'>No settlements recorded yet.</p>"
    rendered = []
    for row in rows:
        outcome = row.get("outcome")
        if outcome in (1, True):
            outcome_label = "win"
        elif outcome in (0, False):
            outcome_label = "loss"
        else:
            outcome_label = "unknown"
        rendered.append({
            "ticker": row.get("ticker", ""),
            "outcome": outcome_label,
            "entry_price": _fmt_cents(row.get("entry_price_cents")),
            "closing_consensus": _fmt_cents(row.get("closing_consensus_cents")),
            "clv": _fmt_cents(row.get("clv_cents")),
        })
    return _table(rendered, ["ticker", "outcome", "entry_price", "closing_consensus", "clv"])


def _signal_engine_rows(ctx: Context) -> list[dict[str, Any]]:
    try:
        summary = ResearchStore(ctx.settings.research_db_path).signal_engine_summary()
    except Exception:
        summary = {}
    rows = []
    for source in ("consensus", "qual"):
        row = summary.get(source) or {}
        rows.append({
            "engine": source,
            "trades_placed": row.get("trades_placed", 0),
            "settled": row.get("settled", 0),
            "brier": "n/a" if row.get("brier") is None else row.get("brier"),
            "clv_beat_rate": "n/a" if row.get("clv_beat_rate") is None else row.get("clv_beat_rate"),
            "clv_available": row.get("clv_available", 0),
        })
    return rows


def _fill_rate_rows(ctx: Context) -> list[dict[str, Any]]:
    try:
        buckets = ResearchStore(ctx.settings.research_db_path).fill_rate_summary().get("buckets") or []
    except Exception:
        buckets = []
    rows = []
    for row in buckets:
        placed = int(row.get("placed") or 0)
        filled = int(row.get("filled") or 0)
        rows.append({
            "signal_source": row.get("signal_source"),
            "liquidity_role": row.get("liquidity_role"),
            "placed": placed,
            "filled": filled,
            "filled_contracts": row.get("filled_contracts", 0),
            "fill_rate": "n/a" if placed <= 0 else f"{filled / placed * 100:.1f}%",
            "canceled_unfilled": row.get("canceled_unfilled", 0),
        })
    return rows


def _qual_learning(ctx: Context) -> dict[str, Any]:
    try:
        return ResearchStore(ctx.settings.research_db_path).qual_learning_summary()
    except Exception:
        return {
            "postmortems_completed": 0,
            "lessons_stored": 0,
            "recaps_found": 0,
            "recaps_missing": 0,
            "calibration": [],
        }


def _qual_calibration_rows(learning: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in learning.get("calibration") or []:
        rows.append({
            "bucket": row.get("bucket"),
            "count": row.get("count", 0),
            "correct": row.get("correct", 0),
            "correct_rate": "n/a" if row.get("correct_rate") is None else f"{float(row.get('correct_rate')) * 100:.1f}%",
            "observed_rate": "n/a" if row.get("observed_rate") is None else f"{float(row.get('observed_rate')) * 100:.1f}%",
            "brier": "n/a" if row.get("brier") is None else row.get("brier"),
        })
    return rows


def _qual_rag(ctx: Context) -> dict[str, Any]:
    try:
        return ResearchStore(ctx.settings.research_db_path).qual_rag_corpus_summary()
    except Exception:
        return {"chunk_count": 0, "source_counts": {}}


def _qual_rag_rows(rag: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"metric": f"source:{key}", "value": value}
        for key, value in sorted((rag.get("source_counts") or {}).items())
    ]
    rows.extend([
        {"metric": "retrievals", "value": rag.get("retrieval_count", 0)},
        {"metric": "leaked_contexts", "value": rag.get("leaked_context_count", 0)},
        {"metric": "mean_groundedness", "value": rag.get("mean_groundedness", "n/a")},
        {"metric": "unsupported_branch_rate", "value": rag.get("unsupported_branch_rate", "n/a")},
        {"metric": "low_groundedness_blocked", "value": rag.get("low_groundedness_blocked", 0)},
    ])
    return rows


def _metric(label: str, value: str, note: str = "") -> str:
    note_html = f"<small>{html.escape(note)}</small>" if note else ""
    return (
        "<section class='metric'>"
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(value)}</strong>"
        f"{note_html}</section>"
    )


def render_dashboard(ctx: Context) -> str:
    artifacts = _load_artifacts(ctx)
    ranker = artifacts.get("candidate_ranker") or {}
    learning = _performance_learning(artifacts)
    validation = _validation_report(ctx, artifacts)
    validated = _validated_families(learning)
    settlement_rows, settlement_count = _settlement_rows(ctx, artifacts)
    signal_engine_rows = _signal_engine_rows(ctx)
    fill_rate_rows = _fill_rate_rows(ctx)
    qual_learning = _qual_learning(ctx)
    qual_rag = _qual_rag(ctx)
    candidates = _candidate_rows(ranker)
    total_candidates = int(ranker.get("candidate_count") or len(candidates))
    priced_count = sum(1 for row in candidates if _candidate_price(row) is not None)
    edge_pass_count = int(ranker.get("edge_pass_count") or 0)
    suspect_count = sum(
        1 for row in candidates
        if any(str(blocker).startswith("edge exceeds plausible maximum") for blocker in (row.get("blockers") or []))
    )
    exec_state = _execution_state(ctx)
    sports = artifacts.get("slate_candidates", {}).get("sports") or []
    sport_label = ", ".join(str(s) for s in sports) if sports else getattr(ctx.settings, "sport", "")
    generated_at = ranker.get("generated_at") or artifacts.get("slate_candidates", {}).get("generated_at") or "n/a"
    provider_rows = _provider_status_rows(artifacts)
    concentration = validation.get("overall_concentration") or {}

    metrics_html = "".join([
        _metric("Candidates Priced", f"{priced_count:,} / {total_candidates:,}"),
        _metric("Edges Found", f"{edge_pass_count:,}"),
        _metric("Flagged Suspect", f"{suspect_count:,}", "plausible-edge guard"),
        _metric("Validated Families", f"{len(validated):,}"),
        _metric("Settled Trades", f"{settlement_count:,}"),
        _metric("CLV Measured", f"{sum(int(row.get('clv_measured_count') or 0) for row in validation.get('groups') or []):,}"),
        _metric("Concentration", "flag" if concentration.get("concentration_flag") else "clear"),
        _metric("Qual Postmortems", f"{int(qual_learning.get('postmortems_completed') or 0):,}"),
        _metric("Qual Lessons", f"{int(qual_learning.get('lessons_stored') or 0):,}"),
        _metric("Qual RAG Chunks", f"{int(qual_rag.get('chunk_count') or 0):,}"),
        _metric("Mean Groundedness", str(qual_rag.get("mean_groundedness") or "n/a")),
    ])
    live_class = "ok" if exec_state["live_cleared"] else "blocked"
    live_text = "yes" if exec_state["live_cleared"] else "no"
    capacity_banner = ""
    if not validated:
        capacity_banner = (
            "<section class='banner blocked'>"
            "<strong>Why it can't trade yet:</strong> live broad-slate capacity is 0 because "
            "no market family is validated. Thresholds: "
            f"n&gt;={VALIDATION_MIN_SETTLED} settled trades, "
            f"{int(VALIDATION_MIN_CLV_BEAT_RATE * 100)}% CLV beat rate per family, "
            "and model Brier better than the market baseline."
            "</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi Broad-Slate Edge Engine</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#637083; --line:#d8dee4;
      --bg:#f6f8fa; --panel:#ffffff; --accent:#0b6657; --good:#12633a;
      --warn:#8a5a00; --bad:#a12b2b; --soft:#eef2f6; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      color:var(--ink); background:var(--bg); }}
    header {{ padding:18px 24px; border-bottom:1px solid var(--line); background:var(--panel);
      display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }}
    h1 {{ font-size:20px; margin:0; letter-spacing:0; }}
    .subhead {{ color:var(--muted); margin:4px 0 0; }}
    .statusbar {{ display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; max-width:760px; }}
    .pill {{ border:1px solid var(--line); border-radius:8px; padding:6px 8px; background:var(--soft); }}
    .pill strong {{ margin-left:4px; }}
    .pill.ok strong {{ color:var(--good); }}
    .pill.blocked strong {{ color:var(--bad); }}
    main {{ max-width:1200px; margin:0 auto; padding:20px 24px 40px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ color:var(--muted); display:block; font-size:12px; }}
    .metric strong {{ font-size:22px; }}
    .metric small {{ display:block; color:var(--muted); margin-top:2px; }}
    .banner {{ margin:0 0 14px; padding:12px 14px; border:1px solid var(--line); border-radius:8px;
      background:var(--panel); }}
    .banner.blocked {{ border-color:#e7c2c2; background:#fff6f6; }}
    section.block {{ margin-top:18px; background:var(--panel); border:1px solid var(--line);
      border-radius:8px; padding:14px; overflow:auto; }}
    h2 {{ font-size:16px; margin:0 0 10px; }}
    table {{ width:100%; border-collapse:collapse; min-width:680px; }}
    th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:7px 8px; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; }}
    .empty {{ color:var(--muted); }}
    tr.eligible td:nth-child(9) {{ color:var(--good); font-weight:700; }}
    tr.flagged td:nth-child(9) {{ color:var(--warn); font-weight:700; }}
    tr.blocked td:nth-child(9) {{ color:var(--bad); font-weight:700; }}
    .footnote {{ color:var(--muted); font-size:12px; margin-top:16px; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Kalshi Broad-Slate Edge Engine</h1>
      <p class="subhead">Read-only view of candidate-ranker, provider health, validation, and settlements.</p>
    </div>
    <div class="statusbar">
      <span class="pill">Run mode <strong>{html.escape(exec_state["mode"])}</strong></span>
      <span class="pill {live_class}">Cleared for live <strong>{live_text}</strong></span>
      <span class="pill">Last scan <strong>{html.escape(str(generated_at))}</strong></span>
      <span class="pill">Sport <strong>{html.escape(str(sport_label or "n/a"))}</strong></span>
      <span class="pill">Evaluated <strong>{total_candidates:,}</strong></span>
    </div>
  </header>
  <main>
    {capacity_banner}
    <div class="metrics">{metrics_html}</div>
    <section class="block"><h2>Edge Candidates</h2>{_render_edge_table(candidates)}</section>
    <section class="block"><h2>Signal Engines</h2>{_table(signal_engine_rows, ["engine","trades_placed","settled","brier","clv_beat_rate","clv_available"])}</section>
    <section class="block"><h2>Validation</h2>{_table(_validation_rows(validation), ["market_family","signal_source","settled","wins","pnl","clv_measured","clv_beat_rate","avg_clv","distance"])}</section>
    <section class="block"><h2>Fill Rate</h2>{_table(fill_rate_rows, ["signal_source","liquidity_role","placed","filled","filled_contracts","fill_rate","canceled_unfilled"])}</section>
    <section class="block"><h2>Qual Learning</h2>{_table(_qual_calibration_rows(qual_learning), ["bucket","count","correct","correct_rate","observed_rate","brier"])}</section>
    <section class="block"><h2>Qual Retrieval</h2>{_table(_qual_rag_rows(qual_rag), ["metric","value"])}</section>
    <section class="block"><h2>Data Source Health</h2>{_table(provider_rows, ["provider","configured","state","rows","remaining_credits","detail"])}</section>
    <section class="block"><h2>Recent Settlements</h2>{_render_settlements(settlement_rows)}</section>
    <p class="footnote">Artifacts are loaded from the newest matching files in {html.escape(str(ctx.settings.data_dir))}. No API keys or secret values are rendered.</p>
  </main>
</body>
</html>"""


def make_handler(ctx: Context):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: str) -> None:
            encoded = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/status":
                self._send(200, "application/json", json.dumps(_load_artifacts(ctx), default=_json_default))
                return
            if self.path not in ("/", "/index.html"):
                self._send(HTTPStatus.NOT_FOUND, "text/plain", "not found")
                return
            self._send(200, "text/html; charset=utf-8", render_dashboard(ctx))

        def do_POST(self) -> None:  # noqa: N802
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain", "dashboard is read-only")

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def serve(ctx: Context) -> None:
    server = ThreadingHTTPServer((ctx.settings.ui_host, ctx.settings.ui_port), make_handler(ctx))
    print(f"[ui] http://{ctx.settings.ui_host}:{ctx.settings.ui_port}", flush=True)
    server.serve_forever()
