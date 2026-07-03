"""Phase: ports. Emit the sport adapter roadmap registry."""
from __future__ import annotations

from ..alerts import deliver
from ..sports import list_ports
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    ports = list_ports()
    payload = {
        "product": "Kalshi Sports Orderbook Engine",
        "compatibility_package": "nbabot",
        "preferred_cli": "ksobot",
        "ports": ports,
    }
    ctx.write_json("sports_ports.json", payload)

    active = sum(
        1 for port in ports
        if port["status"] in {"active-runtime", "research-only"}
    )
    planned = sum(1 for port in ports if port["status"] == "planned")
    deliver(
        f"[ports] {len(ports)} sport ports: active/research={active}, planned={planned}",
        ctx.settings.deliver_to,
    )
    return payload
