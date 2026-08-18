# Codex prompt: demo-cycle cron job stalled again despite the TCC fix landing

## 1. What's real

8-hourly oversight check (2026-08-18) fetched
`https://gist.githubusercontent.com/ellingtonfagan/01680ea910e51feb0bb95183f841efcb/raw`:

```
# monitor NBA-2026-FINALS-G3
- severity: red reasons=no successful cycle in >8h
## alive
- alive: False last_success_hours=15.4828
- last_cycle: 2026-08-17T23:37:45.180069-04:00 edges=2 orders=0 hard_error=None
- host: EllingtonsMBP4 commit=40b1b5374d79aca6254ec7565c611d523a2b2152
## health
- health_alert: False reasons=none
- delivery: failing=False consecutive_failures=0 last_success=2026-08-18T18:14:22.532227+00:00
- escalated: no successful cycle in >8h
- errors: none
## performance
- trends: ... cycle_success=0.7857 ...
```

Key facts, all read directly off this snapshot:

- `alive: False`, `last_success_hours=15.4828` — no cycle has finished (successfully
  *or* with a hard error) since `2026-08-17T23:37:45-04:00`. `_cycle_summary()`
  (`src/nbabot/monitor.py:137-186`) derives `last_cycle_*` fields from the most
  recent `"finished"` row regardless of outcome, so a frozen `last_cycle`
  timestamp means zero cycle attempts have completed at all in that window, not
  just zero successful ones.
- `health_alert: False`, `errors: none`, and **`delivery: failing=False`
  with `last_success=2026-08-18T18:14:22Z`** — the monitor's own reporting path
  is fresh and healthy at the moment this snapshot was generated. This rules out
  "the whole host is asleep/offline," which was the finding in the prior stale-
  branch investigation (`docs/proposed-phases/oversight-20260805-cycles-stopped-stale-branch.md`).
  This time the monitor is alive and delivering; only the demo-cycle production
  path has gone silent.
- The `host commit=40b1b5374d79aca6254ec7565c611d523a2b2152` string does not
  resolve to any commit in this repo's history (`git cat-file -t` fails on it,
  and it doesn't appear in `git log --all`). It cannot be used to confirm which
  code the host is actually running; treat it as unverifiable rather than
  assuming staleness from it.
- `trends.cycle_success=0.7857` over the trailing 7 days is what's driving the
  generic "Cycles are failing at high rate" recommendation
  (`build_recommendations`, `src/nbabot/monitor.py:695-697`, fires below `0.80`).
  That's a separate, softer signal from the acute red condition above and isn't
  this prompt's focus.

## 2. Suspected root cause(s)

`git log --oneline -- scheduler/` shows the most recent scheduler change is
`653905d "Fix scheduled demo cycle silently blocked by macOS TCC (#16)"`,
merged **2026-08-17 20:11:56 -04:00** — about 3.5 hours before the last
successful cycle in the gist (`2026-08-17T23:37:45-04:00`, the `23:35` ET
cron slot). That timing strongly suggests the `23:35` cycle was the *first*
one to run after that fix landed, and it worked. The commit's own message
documents that the prior 3-day outage (2026-08-12 onward) was macOS TCC
denying `/bin/bash` read access to `run-demo-cycle.sh`, fixed by rewriting the
entrypoint as `#!/Users/.../.venv/bin/python3.14` (`scheduler/run-demo-cycle.sh:1-24`,
confirmed present on `main`).

Since that first post-fix success, the scheduled `11:15` ET slot (and possibly
`16:10`) should have produced at least one more `"finished"` row by the time
this snapshot was taken (`last_success_hours=15.48` puts "now" at roughly
`15:06` ET on 2026-08-18, well past `11:15`) — and none did. Two things point
away from a TCC recurrence and toward a different, well-known macOS gotcha:

- `scheduler/combined-crontab.txt:20-24` schedules the four demo-cycle runs
  (and news-watch, and telegram-bot) as plain **cron** entries (`CRON_TZ`,
  `15 11 * * *`, etc.).
- `scheduler/monitor.plist` and `scheduler/meta-check.plist` instead use
  **launchd** with `StartInterval` (7200s / 1800s respectively). launchd's
  `StartInterval`/`StartCalendarInterval` jobs are documented to run shortly
  after wake if the trigger time was missed while the Mac was asleep; vanilla
  cron has no such catch-up — a job whose exact trigger minute falls during
  sleep is simply skipped, with nothing logged.

That split exactly matches the symptom: `monitor` (launchd) kept delivering
fresh snapshots and `health_alert`/`delivery` stayed clean, while the
demo-cycle (pure cron, only 4 fixed slots/day) went silent starting with
whichever slot fell during a sleep window. `src/nbabot/agents/meta_check.py:87-137`
only checks the monitor's *own* heartbeat file, gist reachability, and whether
`com.nbabot.monitor` is loaded in `launchctl list` — it has no visibility into
whether the cron-only demo-cycle jobs are actually firing on their schedule,
so this exact failure mode (cron silently skipped, launchd fine, meta-check
green) can recur indefinitely and only ever surfaces ~8-15h later via the
blunt `no_successful_cycle_gt_8h` check in `evaluate_severity`
(`src/nbabot/monitor.py:639-643`).

This is the leading hypothesis, not a confirmed one — this session has no SSH
access to `EllingtonsMBP4` and cannot read `pmset -g log`, real cron logs, or
`launchctl list` output to prove the Mac slept through `11:15` ET. A TCC
recurrence (e.g. a permission reset) is a less likely but not-impossible
alternative, since the fix's own success at `23:35` doesn't guarantee the
grant persists across every subsequent invocation.

## 3. Proposed fix

Small, scoped, additive — no trading/risk/execution code touched:

1. **New `scheduler/demo-cycle.plist`** — a launchd agent (label
   `com.nbabot.demo-cycle`) mirroring the existing style of
   `scheduler/monitor.plist` / `scheduler/meta-check.plist`, using
   `StartCalendarInterval` with four entries (11:15, 16:10, 18:40, 23:35
   America/New_York — note `StartCalendarInterval` is evaluated in the host's
   local timezone, so confirm the Mac's system timezone is ET, or set it
   explicitly if launchd's plist schema supports it) that invoke the already
   TCC-fixed `scheduler/run-demo-cycle.sh` exactly as the cron entries do
   today. This gives the demo-cycle job the same sleep-resilient catch-up
   behavior `monitor`/`meta-check` already have on this host.
2. **`scheduler/combined-crontab.txt`** — remove the four demo-cycle cron
   lines (`15 11 * * *`, `10 16 * * *`, `40 18 * * *`, `35 23 * * *`) and
   replace them with a comment pointing at `demo-cycle.plist`, so cron and
   launchd don't both fire the same cycle once the plist is loaded (that would
   double-run cycles, burning through `qual_daily_trade_cap` twice as fast).
   Leave the `news-watch` and `telegram-bot` cron lines untouched — this
   prompt does not have evidence either of those is affected, and
   `tests/test_smoke.py::test_demo_scheduler_files_force_demo_without_live_gates`
   reads a *different* file (`scheduler/demo-crontab.txt`, not
   `combined-crontab.txt`) so it's unaffected by this change either way —
   confirm that by running it, don't just assume.
3. **`src/nbabot/monitor.py`** — add one additive, narrowly-scoped signal so a
   missed cron/launchd slot is visible well before the blunt 8h rule fires,
   and is labeled distinctly from a generic "no successful cycle" so a human
   (or the next oversight check) can tell "one slot got skipped" from "the bot
   has been down all day":
   - A small pure helper, e.g. `_next_expected_cycle_slot(now_et, schedule)` or
     `_missed_scheduled_slot_hours(now, last_finished_dt, schedule_et_times)`,
     using the same four ET times as `combined-crontab.txt` (11:15, 16:10,
     18:40, 23:35) as a hardcoded schedule constant near the top of the file.
   - Wire it into `_cycle_summary()` (`src/nbabot/monitor.py:137-186`) as a new
     field, e.g. `"missed_scheduled_slot_hours"` (`None` if the most recent
     expected slot hasn't been missed by more than a small grace window, e.g.
     45 min, to allow for normal run duration).
   - Add **one new yellow-tier** reason in `evaluate_severity`
     (`src/nbabot/monitor.py:627-677`), e.g.
     `("missed_scheduled_cycle_slot", "expected demo-cycle slot at HH:MM ET was skipped")`
     — yellow, not red, and purely additive. Do **not** change the existing
     `no_successful_cycle_gt_8h` red condition, its 8h threshold, or any other
     existing reason/threshold.

Do not touch `live_execute.py`, any live-gate env var, `risk.py` thresholds,
`sizing.py`, or `MIN_EDGE` values.

## 4. Tests to add

- `tests/test_smoke.py`: a unit test for the new helper function — given the
  four-slot ET schedule and a `now` that's e.g. 2h past an expected slot with
  no matching `"finished"` row newer than that slot, assert it reports the
  missed slot; given a `now` within the grace window of the most recent slot,
  or a `now` where a fresh `"finished"` row exists after the slot, assert it
  reports `None`/no miss.
- `tests/test_smoke.py`: extend the existing `_cycle_summary`/`evaluate_severity`
  tests (search for existing monitor tests building fake
  `scheduled_cycle_runs.jsonl` rows) with a case asserting the new
  `missed_scheduled_cycle_slot` yellow reason fires standalone (without red)
  when a slot is missed but the bot is otherwise within the 8h red window, and
  that it does **not** fire when all recent slots have matching finished rows.
- Run `tests/test_smoke.py::test_demo_scheduler_files_force_demo_without_live_gates`
  specifically after editing `combined-crontab.txt` to confirm it's unaffected
  (it reads `demo-crontab.txt`, a different file) rather than assuming so.

## 5. Verification checklist

```
.venv/bin/pytest -q                                     # expect: all passing, same count + N new
.venv/bin/pytest -q -k "monitor or cycle_summary or demo_scheduler"
git diff --stat                                          # expect: scheduler/demo-cycle.plist (new),
                                                           #   scheduler/combined-crontab.txt,
                                                           #   src/nbabot/monitor.py,
                                                           #   tests/test_smoke.py only — no changes
                                                           #   to live_execute.py, risk.py, sizing.py,
                                                           #   scheduler/run-demo-cycle.sh,
                                                           #   scheduler/demo-crontab.txt
```

Then a real (non-live) invocation to confirm the new monitor field flows end
to end without changing existing severity behavior:

```
NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot monitor
cat data/monitor.md               # confirm existing severity/reasons unchanged
                                    # for current ledger state, and the new field
                                    # appears in monitor_snapshot.json
python3 -c "import xml.dom.minidom, sys; xml.dom.minidom.parse('scheduler/demo-cycle.plist')"
                                    # confirm the new plist is well-formed XML
```

Look for: no change in the *existing* red/yellow reasons for the current
`data/scheduled_cycle_runs.jsonl` state, the new
`missed_scheduled_cycle_slot`/`missed_scheduled_slot_hours` field present and
`None` when no slot is currently missed, and `demo-cycle.plist` parsing as
valid XML with `com.nbabot.demo-cycle` as its label and the four
`StartCalendarInterval` entries matching `combined-crontab.txt`'s times.
`git diff --stat` matching the file list above — this is additive scheduling +
observability, not a rewrite.

This does not fix a code bug in the trading pipeline — it closes an
operational gap (cron has no sleep catch-up; nothing watched the cron-only
schedule specifically) that let a real outage go undetected for 15+ hours
despite the monitor itself looking healthy the whole time. Actually loading
`scheduler/demo-cycle.plist` via `launchctl load` and removing the four old
cron lines from the *installed* crontab (not just this file) is a manual,
hands-on-host step this session cannot perform — call that out explicitly in
the PR/handoff so it isn't mistaken for already done once this merges.

## 6. Guardrails

No commit/push. No live env vars set. No live orders. Do not edit live gates.
