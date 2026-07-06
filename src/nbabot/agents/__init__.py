"""Game-phase and research/execution agents."""

from . import (  # noqa: F401
    autopilot,
    backtest,
    baseline,
    book_watch,
    demo_execute,
    discover_markets,
    heartbeat,
    lineups,
    live_execute,
    lock,
    paper,
    ports,
    reconcile,
    snapshot_market,
    telegram_test,
    ui,
)

PHASES = {
    "autopilot": autopilot.run,
    "backtest": backtest.run,
    "baseline": baseline.run,
    "book-watch": book_watch.run,
    "demo-execute": demo_execute.run,
    "discover-markets": discover_markets.run,
    "lineups": lineups.run,
    "live-execute": live_execute.run,
    "lock": lock.run,
    "heartbeat": heartbeat.run,
    "live": heartbeat.run,      # alias so a plain crontab can drive the live loop
    "paper": paper.run,
    "ports": ports.run,
    "reconcile": reconcile.run,
    "snapshot-market": snapshot_market.run,
    "telegram-test": telegram_test.run,
    "ui": ui.run,
}
