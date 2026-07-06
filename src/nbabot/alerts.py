"""Compact-block formatting + delivery. Keep alerts terse (§4 of the skill)."""
from __future__ import annotations

import json
import os

import requests

from .scenarios import ScenarioState
from .triggers import TriggerHit


def format_block(header: str, scen_states: list[ScenarioState],
                 triggers: list[TriggerHit]) -> str:
    lines = [header]
    for ss in scen_states:
        x = f"~{ss.live_payout_x:g}x" if ss.live_payout_x else "n/a"
        lines.append(f"  {ss.id} {ss.state:9s} {ss.hit_legs}/{ss.total_legs} legs  live {x}"
                     + (f"  ({ss.note})" if ss.note else ""))
    for t in triggers:
        lines.append(f"ALERT: {t.message}")
    if not triggers:
        lines.append("ALERT: none")
    return "\n".join(lines)


def deliver(text: str, to: str = "stdout") -> None:
    if to == "stdout" or not to:
        print(text)
        return
    if to.startswith("telegram"):
        token = os.environ.get("NBABOT_TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("NBABOT_TELEGRAM_CHAT_ID", "")
        if ":" in to and not chat_id:
            chat_id = to.split(":", 1)[1].strip()
        if not token or not chat_id:
            print("[deliver telegram missing NBABOT_TELEGRAM_BOT_TOKEN/NBABOT_TELEGRAM_CHAT_ID]\n" + text)
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({
                    "chat_id": chat_id,
                    "text": text[:4096],
                    "disable_web_page_preview": True,
                }),
                headers={"Content-Type": "application/json"},
                timeout=6,
            )
        except Exception as e:  # delivery must never crash a run
            print(f"[deliver telegram failed: {e}]\n{text}")
        return
    if to.startswith("http"):
        try:
            requests.post(to, data=json.dumps({"text": text}),
                          headers={"Content-Type": "application/json"}, timeout=6)
        except Exception as e:  # delivery must never crash a run
            print(f"[deliver webhook failed: {e}]\n{text}")
        return
    print(f"[deliver target '{to}' unknown, printing]\n{text}")
