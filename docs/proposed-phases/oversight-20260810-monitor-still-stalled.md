# Oversight 2026-08-10: monitor pipeline still frozen (~160.5h), continuing from #12 (merged)

**Type: investigation-only, continuation.** Predecessor PR #12
(`oversight-20260805-cycles-stopped-stale-branch`) tracked this exact same
condition across 17 consecutive 8-hourly checks (2026-08-05 through
2026-08-10T12:36 UTC) and was **merged into main at 2026-08-10T20:02:21 UTC**,
~6 minutes before this check ran. The underlying host condition it documented
is unchanged — opening a fresh doc/PR to continue the paper trail since there
is no longer an open PR to update.

## What's real

Current gist (fetched 2026-08-10T20:08 UTC, eighteenth check overall):

```
severity: yellow reasons=last cycle placed 0 orders and had trade-eligible edges; currently-running phase is 172.3h old
alive: True last_success_hours=0.7993
last_cycle: 2026-08-03T23:37:22.048421-04:00 edges=2 orders=0 hard_error=None
host: Ellingtons-MacBook-Pro-4.local commit=0d9ae5d37e93b563321c4f03c172e5ff4001385a
pending_prs: 3,4,5,6,7,8
```

**Byte-identical** to every check recorded in #12: same `last_cycle`, same
host commit (still the unmerged `phase-25-oversight` tip), same
`pending_prs: 3,4,5,6,7,8`, same reasons text. `now - last_cycle` ≈
**160.5h** (was 153.0h at check seventeen) — ~6.69 calendar days of zero
recorded cycles.

## Suspected root cause(s)

No new evidence beyond what #12 already established (see that PR body /
`docs/proposed-phases/oversight-20260805-cycles-stopped-stale-branch.md` on
main for the full chain):

- `src/nbabot/agents/meta_check.py` heartbeat watchdog likely never installed
  on the host (its launchd agent is a manual post-merge step per PR #8, which
  is still open/unmerged).
- Host checkout is still pinned to `0d9ae5d3...`, the tip of unmerged
  `origin/phase-25-oversight` (PR #8) — several days behind `main`, missing
  the phantom-exposure fix (#9) and later merges.

## Proposed fix

Still none to make blind — this needs the same hands-on-host verification
steps #12 already listed (`launchctl list | grep nbabot`, confirm host is
awake/online, check actual local branch/HEAD vs the gist's claimed commit,
inspect `data/monitor_heartbeat.txt` / `data/scheduled_cycle_runs.jsonl`).
Nothing here changes that list; repeating it would be duplicative of the
merged doc.

## Tests to add

Per #12's original suggestion, still open: a monitoring-side self-staleness
check — `src/nbabot/monitor.py` should compare its own generation time
against `data/monitor_heartbeat.txt` and flag itself (not just the trading
pipeline) as stale if the gap exceeds a threshold, independent of whatever
`last_cycle` says. No test exists for this yet.

## Verification checklist

- `.venv/bin/pytest -q` on the host — confirm still ~190 passing / 5
  pre-existing fixture failures (baseline from #12, unchanged by this doc-only
  branch).
- On the host: `launchctl list | grep nbabot`, `git -C <repo> status`,
  `git -C <repo> log -1 --format='%H %cI'` — do these match the gist's claimed
  commit and timestamp, or has local state already moved and only the gist
  publish step is broken?
- `git diff --stat` on this branch should show exactly one file added
  (this doc) — no source changes.

## Guardrails

No commit/push beyond this doc. No live env vars set. No live orders. Do not
edit live gates (`src/nbabot/agents/live_execute.py`, live gate env vars,
`risk.py` thresholds, `sizing.py`, `MIN_EDGE` values).

## Re-notification trigger (carried over from #12, check eleven)

Re-escalate to the user only when one of: severity flips to **red**,
`now - last_cycle` crosses **168h (7 calendar days)** — projected to happen
at or before the **next** check (~168.5h) — or any change in
`last_cycle` / `host commit` / `pending_prs` (meaning the host came back).
None of those fired this round (160.5h < 168h, all three fields identical to
check seventeen), so this check updates the doc/PR silently per the
established plan; the next check is very likely to cross the threshold and
should notify.
