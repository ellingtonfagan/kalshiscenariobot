"""Compact-block formatting + delivery. Keep alerts terse (§4 of the skill)."""
from __future__ import annotations

import json

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
    if to.startswith("http"):
        try:
            requests.post(to, data=json.dumps({"text": text}),
                          headers={"Content-Type": "application/json"}, timeout=6)
        except Exception as e:  # delivery must never crash a run
            print(f"[deliver webhook failed: {e}]\n{text}")
        return
    print(f"[deliver target '{to}' unknown, printing]\n{text}")
