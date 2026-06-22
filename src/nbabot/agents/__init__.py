"""Game-phase and research/execution agents."""

from . import (  # noqa: F401
    autopilot,
    backtest,
    baseline,
    demo_execute,
    discover_markets,
    heartbeat,
    lineups,
    live_execute,
    lock,
    paper,
    reconcile,
    snapshot_market,
    ui,
)

PHASES = {
    "autopilot": autopilot.run,
    "backtest": backtest.run,
    "baseline": baseline.run,
    "demo-execute": demo_execute.run,
    "discover-markets": discover_markets.run,
    "lineups": lineups.run,
    "live-execute": live_execute.run,
    "lock": lock.run,
    "heartbeat": heartbeat.run,
    "live": heartbeat.run,      # alias so a plain crontab can drive the live loop
    "paper": paper.run,
    "reconcile": reconcile.run,
    "snapshot-market": snapshot_market.run,
    "ui": ui.run,
}
