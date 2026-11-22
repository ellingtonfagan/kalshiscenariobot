# Codex prompt: monitor gist freshness is invisible, so a dead publisher reads as "red, no cycle in 8h" forever

## 1. What's real

Current gist (`https://gist.githubusercontent.com/ellingtonfagan/01680ea910e51feb0bb95183f841efcb/raw`), fetched 2026-11-22:

```
# monitor NBA-2026-FINALS-G3
- severity: red reasons=no successful cycle in >8h
## alive
- alive: False last_success_hours=10.845
- last_cycle: 2026-08-27T00:08:35.765807-04:00 edges=0 orders=0 hard_error=None
- host: Ellingtons-MacBook-Pro-4.local commit=40b1b5374d79aca6254ec7565c611d523a2b2152
```

`last_success_hours=10.845` against `last_cycle=2026-08-27T00:08:35`, implies this
snapshot was *generated* around `2026-08-27T10:5x` ET. Today is 2026-11-22 — roughly
87 days / ~2088 hours after that generation time, not 10.845 hours. The gist has not
changed since. This is not "bot idle for 8h", it is "the process that publishes this
gist stopped running months ago," and nothing in the pipeline says so.

Corroborating: `git log` shows the last commit on `main` (`be48316`) and the last
`oversight-*` PR (`#19`, merged `770416c`) both landed 2026-08-18 — this recurring
8-hourly check itself did not fire again until now either. Both the bot host and the
oversight check went dark around the same window. The repo-fixable part of this is
the gist/monitor side; the scheduled-check gap is a Claude Code Remote trigger issue
outside this repo and is being raised to the user directly, not via this PR.

Note: this echoes the Aug 2026 "frozen gist" incident fixed by PR #12
(`oversight-20260805-cycles-stopped-stale-branch`) — that fix addressed a stale
branch, not gist-staleness detection, so the underlying blind spot survived.

## 2. Suspected root cause(s)

1. **`format_monitor_md` never emits `generated_at`** —
   `src/nbabot/monitor.py:837-851` (`format_monitor_md`). `MonitorSnapshot` carries a
   `generated_at` field (`src/nbabot/monitor.py:25`, populated at
   `src/nbabot/monitor.py:761`), but the markdown renderer only prints
   `last_success_hours` and `last_cycle` (both derived from cycle data, not from when
   the snapshot itself was produced). A reader of the published gist — human or this
   oversight check — has no field to tell "snapshot is fresh, bot genuinely idle" apart
   from "snapshot itself is months old because nothing has republished it."

2. **`meta_check.py`'s gist-plausibility check is a trivially-passing year substring
   match** — `src/nbabot/agents/meta_check.py:108-112`:
   ```python
   recent_marker = str(now.year) in gist_body or now.strftime("%Y-%m-%d") in gist_body
   if "monitor" not in body_lower or not recent_marker:
       failures.append("monitor gist content is not plausible")
   ```
   Any gist generated earlier in the *same calendar year* contains `"2026"` somewhere
   (e.g. in `last_cycle: 2026-08-27...`), so `recent_marker` is `True` for a snapshot
   that is months stale, as long as the clock hasn't rolled into a new year. This check
   cannot catch the exact failure mode in section 1 — it would pass right now, feeding
   this gist straight through as "plausible." (Also: `meta_check.py` runs on the same
   host as the bot, so if the host itself is down, `meta_check` doesn't run either —
   this check only protects against the monitor process dying while the host stays up.)

## 3. Proposed fix

Scope: `src/nbabot/monitor.py` and `src/nbabot/agents/meta_check.py` only. No
cross-cutting refactor. Do not touch `live_execute.py`, any live-gate env var,
`risk.py` thresholds, `sizing.py`, or `MIN_EDGE` values.

- In `src/nbabot/monitor.py`, add a `generated_at` line to `format_monitor_md`'s
  output, e.g. immediately after the `## alive` header or next to the `host:` line:
  ```python
  f"- generated_at: {s['generated_at']}",
  ```
  (Confirm the exact key `MonitorSnapshot.to_dict()` uses — it's `generated_at` per
  the dataclass field at `src/nbabot/monitor.py:25`.)

- In `src/nbabot/agents/meta_check.py`, replace the substring-based `recent_marker`
  check (lines 108-112) with one that actually parses a `generated_at` (or equivalent)
  timestamp out of the gist body and compares it to `now`, failing when it's older
  than a small threshold (e.g. 1-2 hours, matching the monitor's own publish cadence —
  check how often the publishing cron/launchd job is expected to run and use that as
  the bound, not an arbitrary guess). Reuse `_parse_dt` already defined in that file.
  If no parseable `generated_at` line is found in the gist body at all, that itself
  should be a failure ("monitor gist missing generated_at"), not a silent pass.

## 4. Tests to add

- `tests/test_smoke.py` (where the existing `format_monitor_md` assertion lives, near
  line 8049): assert the rendered markdown contains a `generated_at:` line matching
  `snapshot.generated_at`.
- A new focused test for `meta_check.run` (or a pure helper extracted from it) that
  constructs a gist body whose `generated_at`-equivalent timestamp is e.g. 90 days old
  but which still contains the current year string elsewhere (mirroring the real gist
  above), and asserts the check now reports `"monitor gist content is not plausible"`
  (or a clearer message like `"monitor gist is stale"`) instead of passing. This is the
  test that would have caught the exact defect described in section 2 — the old
  `now.year in gist_body` check would pass it today; the fixed check must fail it.
- A second case with a genuinely fresh `generated_at` (a few minutes old) should still
  pass, to confirm the new check isn't just always-failing.

## 5. Verification checklist

- `.venv/bin/pytest -q` — expect all existing tests still pass plus the new ones
  (baseline before this change was `149 passed` per the most recent progress-doc
  entry; confirm the new total includes +2/+3 for the added cases).
- Run a real invocation that exercises `format_monitor_md` end-to-end, e.g. whatever
  `ksobot` phase/status command renders the monitor markdown (check `src/nbabot/agents/monitor.py`
  for the actual CLI entry point used in production), and visually confirm the output
  now includes a `generated_at:` line with a real current timestamp.
- `git diff --stat` expectation: changes confined to `src/nbabot/monitor.py`,
  `src/nbabot/agents/meta_check.py`, and the test file(s) above — no changes to
  `live_execute.py`, `risk.py`, `sizing.py`, or any live-gate env var plumbing.

## 6. Guardrails

No commit/push. No live env vars set. No live orders. Do not edit live gates.
