# Monitor phase

`ksobot monitor` composes the bot's current operating state into:

- `data/monitor_snapshot.json`: deterministic machine-readable snapshot.
- `data/monitor.md`: compact operator summary, kept short enough for phone review.
- `data/monitor_heartbeat.txt`: UTC timestamp written before each monitor run.

It reads local artifacts, SQLite ledgers, and best-effort GitHub PR state. It does
not place orders, set live environment variables, or change risk/execution gates.

For launchd, copy `scheduler/monitor.plist` into `~/Library/LaunchAgents/` and load
it yourself after reviewing paths. It runs every two hours and leaves the demo-cycle
cron entries untouched.

`ksobot meta-check` is a separate watcher for monitor silence. It checks heartbeat
freshness, public gist reachability/content, and whether `com.nbabot.monitor` appears
in `launchctl list`; any failure sends an escalated Telegram alert directly.

To install it after review:

```bash
cp scheduler/meta-check.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/meta-check.plist
```

## Oversight phase

`ksobot oversight` runs a dual-engine review over local artifacts:

- **Codex CLI** (primary): `NBABOT_QUAL_LLM_CMD` — same subprocess bridge as qual-research.
- **Claude API** (fallback): `ANTHROPIC_API_KEY` + `NBABOT_QUAL_FALLBACK_MODEL` when Codex is
  unavailable (timeout, usage limit, missing command).

It reads `monitor_snapshot.json`, `daily_cycle.json`, qual/validation/status artifacts, and writes
`data/<GAME_ID>.oversight_report.json`. It does not place orders or change execution gates.

Run after `monitor` or at the end of a demo cycle:

```bash
ksobot monitor
ksobot oversight
```

Oversight alerts include the standard guardrail footer; recommendations are operational only.
