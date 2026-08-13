# Claude Handoff - 2026-08-11 Telegram Status

Timestamp: 2026-08-11 21:16 EDT
Workspace: `/Users/ellingtonfagan/Downloads/nba-scenario-bot`

## User request

Build a `/status` function inside the Telegram bot so status can be fetched from Telegram without asking Codex or Claude to inspect the repo.

## What changed

- Added `src/nbabot/telegram_bot.py`.
  - Polls Telegram Bot API with `getUpdates`.
  - Handles `/status`, `/help`, and `/start`.
  - Strips bot mentions such as `/status@bot`.
  - Requires `NBABOT_TELEGRAM_BOT_TOKEN`.
  - Requires `NBABOT_TELEGRAM_CHAT_ID`; without it the phase exits before network calls.
  - Ignores messages from any chat other than the configured chat ID.
  - Stores Telegram offset in `data/telegram_bot_offset.json`.
  - Sends replies through `sendMessage`.
  - Truncates replies to Telegram's 4096 character limit.

- Added `src/nbabot/agents/telegram_bot.py`.
  - New agent phase wrapper around the core Telegram polling module.
  - Writes phase output to `data/<GAME_ID>.telegram_bot.json`.

- Registered the new phase in `src/nbabot/agents/__init__.py`.
  - `ksobot telegram-bot` now dispatches to the Telegram polling agent.

- Updated `scheduler/combined-crontab.txt`.
  - Added a 24/7 one-minute cron poll:
    `* * * * * ... NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot telegram-bot >> logs/telegram-bot-$(date +%Y%m%d).log 2>&1`
  - Installed this crontab with:
    `cat scheduler/combined-crontab.txt | crontab -`
  - Verified `crontab -l | tail -8` includes the new `telegram-bot` line.

- Updated `.env.example`.
  - Added `NBABOT_TELEGRAM_POLL_TIMEOUT_SECONDS=0`.
  - The default is cron-style one-shot polling.

- Updated `README.md`.
  - Added `ksobot telegram-bot` to the command list with a short description.

- Updated `tests/test_smoke.py`.
  - Added coverage for `/status` using a local `monitor.md` artifact.
  - Added coverage for ignoring unauthorized chats.
  - Added coverage for refusing to poll when chat ID is not configured.
  - Added coverage that the phase is registered.

## Runtime behavior

When a Telegram user sends `/status`:

1. The cron job runs `ksobot telegram-bot` once per minute.
2. The bot fetches pending Telegram updates.
3. If the update is from `NBABOT_TELEGRAM_CHAT_ID`, it replies.
4. Reply source order:
   - Prefer `data/monitor.md` if present and nonempty.
   - Otherwise build status locally through `src/nbabot/agents/status.py`.
5. Status replies are wrapped with the existing guardrail footer.

This does not call Codex, Claude, or any Codex API.

## Verification performed

Focused tests passed:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_smoke.py::test_telegram_delivery_uses_bot_api_without_network \
  tests/test_smoke.py::test_telegram_bot_status_command_uses_local_monitor_artifact \
  tests/test_smoke.py::test_telegram_bot_ignores_unconfigured_chat_and_phase_registered \
  tests/test_smoke.py::test_telegram_bot_requires_configured_chat_id \
  tests/test_smoke.py::test_new_automation_phases_registered \
  tests/test_smoke.py::test_scheduled_demo_cycle_reconciles_stale_exchange_orders_before_exposure
```

Result:

```text
6 passed in 0.21s
```

Other checks:

```bash
git diff --check
```

Result: passed.

```bash
NBABOT_TELEGRAM_BOT_TOKEN= NBABOT_TELEGRAM_CHAT_ID= NBABOT_EXECUTION_MODE=demo .venv/bin/ksobot telegram-bot
```

Result: wrote `data/NBA-2026-FINALS-G3.telegram_bot.json` with `missing-telegram-token`.

```bash
ps -axo pid,command | rg 'ksobot telegram-bot|pytest' || true
```

Result: no lingering bot or pytest process, aside from the `ps`/`rg` check itself.

## Existing dirty worktree caveat

The repo already had unrelated or pre-existing dirty files before the Telegram work. Do not assume all modified files belong to this change.

Known unrelated or pre-existing items included:

- `.gitignore`
- `docs/edge-engine-progress.md`
- `src/nbabot/agents/__init__.py` had prior monitor/meta-check/oversight edits before the Telegram phase was added.
- `tests/test_smoke.py` had prior monitor/meta-check related edits before Telegram tests were added.
- Untracked runtime/state files under `data/` and `logs/`.
- Untracked monitor/oversight/meta-check files.

Telegram-specific files added in this pass:

- `src/nbabot/telegram_bot.py`
- `src/nbabot/agents/telegram_bot.py`
- `docs/claude-handoff-2026-08-11-telegram-status.md`

Telegram-related edits in existing files:

- `.env.example`
- `README.md`
- `scheduler/combined-crontab.txt`
- `src/nbabot/agents/__init__.py`
- `tests/test_smoke.py`

## Related prior fix from same session

The scheduled demo cycle was also changed earlier in the same session so stale demo exchange orders are reconciled before the daily cycle risk gate computes exposure.

Relevant files:

- `src/nbabot/agents/scheduled_demo_cycle.py`
- `tests/test_smoke.py`

Focused regression passed:

```bash
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_smoke.py::test_scheduled_demo_cycle_reconciles_stale_exchange_orders_before_exposure
```

Context: the original `game_exposure` reject was caused by stale resting demo orders being counted before cleanup. The fix runs `order-reconcile` before `daily-cycle` in the scheduled demo path.
