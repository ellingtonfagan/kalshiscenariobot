"""Small dependency-free local web UI for the bot's artifacts."""
from __future__ import annotations

import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from .research import ResearchStore

if TYPE_CHECKING:
    from .agents.base import Context


def _json_default(obj: Any) -> str:
    return str(obj)


def _load_artifacts(ctx: Context) -> dict[str, Any]:
    return {
        "board": ctx.read_json("board.json") or {},
        "heartbeat": ctx.read_json("hb_state.json") or {},
        "market": ctx.read_json("market_snapshot.json") or {},
        "catalog": ctx.read_json("market_catalog.json") or {},
        "backtest": ctx.read_json("backtest.json") or {},
        "autopilot": ctx.read_json("autopilot.json") or {},
        "paper": ctx.read_json("paper.json") or {},
        "demo": ctx.read_json("demo_execute.json") or {},
        "live": ctx.read_json("live_execute.json") or {},
        "reconcile": ctx.read_json("reconcile.json") or {},
    }


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


def render_dashboard(ctx: Context) -> str:
    store = ResearchStore(ctx.settings.research_db_path)
    artifacts = _load_artifacts(ctx)
    board = artifacts["board"].get("board", {})
    market_rows = artifacts["market"].get("rows", [])
    catalog_rows = artifacts["catalog"].get("rows", [])
    backtest = artifacts["backtest"]
    paper = artifacts["paper"].get("orders", [])
    live = artifacts["live"]
    risk_rows = store.latest_rows("risk_snapshots", 10)
    audit_rows = store.latest_rows("audit_events", 20)

    cards = []
    cards.append(("Game", ctx.settings.game_id))
    cards.append(("Scenarios", str(len(board) or len(ctx.scenarios))))
    cards.append(("Market Rows", str(len(market_rows))))
    cards.append(("Discovered", str(len(catalog_rows))))
    cards.append(("Paper Orders", str(len(paper))))
    cards.append(("Execution Mode", ctx.settings.execution_mode))
    cards.append(("Dry Run", str(ctx.settings.dry_run)))

    scenario_rows = []
    for sid, item in board.items():
        scenario_rows.append({
            "id": sid,
            "name": item.get("name"),
            "risk": item.get("risk"),
            "legs": len(item.get("legs", [])),
        })

    latest_market = [
        {
            "scenario": r.get("scenario_id"),
            "market": r.get("market"),
            "ticker": r.get("ticker"),
            "prior": r.get("prior_p"),
            "implied": r.get("implied"),
            "edge": round(float(r.get("prior_p", 0)) - float(r.get("implied", 0)), 3)
            if r.get("implied") is not None else "",
        }
        for r in market_rows[:25]
    ]

    latest_catalog = [
        {
            "status": r.get("mapping_status"),
            "series": r.get("series"),
            "ticker": r.get("ticker"),
            "player": r.get("player") or r.get("team"),
            "stat": r.get("stat"),
            "line": r.get("line"),
            "mapped": ",".join(r.get("mapped_markets", [])),
        }
        for r in catalog_rows[:25]
    ]

    card_html = "".join(
        f"<section class='metric'><span>{html.escape(k)}</span><strong>{html.escape(v)}</strong></section>"
        for k, v in cards
    )
    backtest_json = html.escape(json.dumps(backtest, indent=2, default=_json_default)[:4000])
    paper_json = html.escape(json.dumps(paper[:5], indent=2, default=_json_default)[:4000])
    live_json = html.escape(json.dumps(live, indent=2, default=_json_default)[:4000])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NBA Scenario Bot</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#65717d; --line:#d8dee4;
      --bg:#f7f9fb; --panel:#ffffff; --accent:#0b6b57; --warn:#9a5b00; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      color:var(--ink); background:var(--bg); }}
    header {{ padding:18px 24px; border-bottom:1px solid var(--line); background:var(--panel);
      display:flex; align-items:center; justify-content:space-between; gap:16px; }}
    h1 {{ font-size:20px; margin:0; letter-spacing:0; }}
    main {{ max-width:1200px; margin:0 auto; padding:20px 24px 40px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric span {{ color:var(--muted); display:block; font-size:12px; }}
    .metric strong {{ font-size:22px; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:8px; }}
    button {{ border:1px solid var(--accent); background:var(--accent); color:white; border-radius:6px;
      padding:8px 11px; font-weight:600; cursor:pointer; }}
    button.secondary {{ background:white; color:var(--accent); }}
    section.block {{ margin-top:18px; background:var(--panel); border:1px solid var(--line);
      border-radius:8px; padding:14px; overflow:auto; }}
    h2 {{ font-size:16px; margin:0 0 10px; }}
    table {{ width:100%; border-collapse:collapse; min-width:680px; }}
    th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:7px 8px; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; }}
    pre {{ white-space:pre-wrap; background:#f1f4f7; padding:10px; border-radius:6px; overflow:auto; }}
    .empty {{ color:var(--muted); }}
    .guardrail {{ color:var(--warn); font-weight:600; }}
  </style>
</head>
<body>
  <header>
    <h1>NBA Scenario Bot</h1>
    <form class="actions" method="post">
      <button formaction="/action/backtest">Backtest</button>
      <button formaction="/action/autopilot" class="secondary">Autopilot</button>
      <button formaction="/action/discover-markets" class="secondary">Discover Markets</button>
      <button formaction="/action/snapshot-market" class="secondary">Snapshot Market</button>
      <button formaction="/action/paper" class="secondary">Paper</button>
      <button formaction="/action/demo-execute" class="secondary">Demo Execute</button>
      <button formaction="/action/live-execute" class="secondary">Live Execute</button>
    </form>
  </header>
  <main>
    <p class="guardrail">Guarded execution. Live trading requires explicit env gates and the risk gate.</p>
    <div class="metrics">{card_html}</div>
    <section class="block"><h2>Scenarios</h2>{_table(scenario_rows, ["id","name","risk","legs"])}</section>
    <section class="block"><h2>Discovered Markets</h2>{_table(latest_catalog, ["status","series","ticker","player","stat","line","mapped"])}</section>
    <section class="block"><h2>Latest Market Snapshot</h2>{_table(latest_market, ["scenario","market","ticker","prior","implied","edge"])}</section>
    <section class="block"><h2>Backtest</h2><pre>{backtest_json}</pre></section>
    <section class="block"><h2>Live Order</h2><pre>{live_json}</pre></section>
    <section class="block"><h2>Paper / Demo Orders</h2><pre>{paper_json}</pre></section>
    <section class="block"><h2>Risk Snapshots</h2>{_table(risk_rows, ["captured_at","daily_pnl_units","game_exposure_units","open_positions","circuit_breaker_on"])}</section>
    <section class="block"><h2>Audit</h2>{_table(audit_rows, ["created_at","event_type","game_id","event_json"])}</section>
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
            from .agents import autopilot, backtest, demo_execute, discover_markets, live_execute, paper, snapshot_market

            actions = {
                "/action/autopilot": autopilot.run,
                "/action/backtest": backtest.run,
                "/action/discover-markets": discover_markets.run,
                "/action/snapshot-market": snapshot_market.run,
                "/action/paper": paper.run,
                "/action/demo-execute": demo_execute.run,
                "/action/live-execute": live_execute.run,
            }
            fn = actions.get(self.path)
            if not fn:
                self._send(HTTPStatus.NOT_FOUND, "text/plain", "not found")
                return
            fn(ctx)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def serve(ctx: Context) -> None:
    server = ThreadingHTTPServer((ctx.settings.ui_host, ctx.settings.ui_port), make_handler(ctx))
    print(f"[ui] http://{ctx.settings.ui_host}:{ctx.settings.ui_port}", flush=True)
    server.serve_forever()
