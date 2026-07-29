# Operating Without Codex

The bot does not need the Codex desktop app to run. Codex is useful for editing and
reviewing the codebase, but production operation should be a normal terminal process
or scheduler.

## Recommended Setup

Use the terminal, cron, launchd, or systemd as the host:

```bash
cd /Users/ellingtonfagan/Downloads/nba-scenario-bot
source .venv/bin/activate
pip install -e .
cp .env.example .env
ksobot ports
ksobot autopilot
```

For repeated runs, use the existing cron file:

```bash
crontab scheduler/combined-crontab.txt
```

`autopilot` is the best repeated operational phase because it safely decides which game
phase to run based on the current game state.

## Telegram Alerts

Telegram should be an alert and command surface, not the process host. The machine
running the terminal/scheduler should still own API keys, risk gates, the kill switch,
SQLite, and audit logs.

To send alerts to Telegram:

1. Create a bot with BotFather.
2. Get your chat ID.
3. Put these values in `.env`:

```bash
NBABOT_DELIVER_TO=telegram
NBABOT_TELEGRAM_BOT_TOKEN=REPLACE_WITH_BOT_TOKEN
NBABOT_TELEGRAM_CHAT_ID=REPLACE_WITH_CHAT_ID
```

You can also set `NBABOT_DELIVER_TO=telegram:<chat_id>` and keep the token in
`NBABOT_TELEGRAM_BOT_TOKEN`.

Then verify delivery:

```bash
ksobot telegram-test
```

## Operating Model

- **Terminal/cron/launchd:** owns the bot process, data directory, audit log, kill switch,
  and execution gates.
- **Telegram:** receives compact alerts and can later be extended with explicit commands
  such as status, pause, resume, and run `book-watch`.
- **Codex:** remains the development assistant for code changes, reviews, migrations, and
  debugging.

Do not run live execution from chat alone. Live execution should still require the same
environment gates, risk checks, and local audit trail.

Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369).
