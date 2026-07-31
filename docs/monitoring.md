# Monitor phase

`ksobot monitor` composes the bot's current operating state into:

- `data/monitor_snapshot.json`: deterministic machine-readable snapshot.
- `data/monitor.md`: compact operator summary, kept under 40 lines.

It reads local artifacts, SQLite ledgers, and best-effort GitHub PR state. It does
not place orders, set live environment variables, or change risk/execution gates.

For launchd, copy `scheduler/monitor.plist` into `~/Library/LaunchAgents/` and load
it yourself after reviewing paths. It runs every two hours and leaves the demo-cycle
cron entries untouched.
