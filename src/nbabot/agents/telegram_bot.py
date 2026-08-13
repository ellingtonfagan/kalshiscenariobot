"""Phase: telegram-bot. Poll Telegram commands once and answer operator requests."""
from __future__ import annotations

from .. import telegram_bot
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    result = telegram_bot.run_once(ctx)
    ctx.write_json("telegram_bot.json", result)
    return result
