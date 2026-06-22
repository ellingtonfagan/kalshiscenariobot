"""Phase: lineups (T-90m).

Confirm inactives/starters. If a tracked key player is OUT, mark every scenario whose
legs depend on that player as VOID and alert. If no real source is wired, say so plainly
(unconfirmed) rather than guessing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import news
from ..alerts import deliver
from .base import Context, load_context


def _player_in_scenario(scenario, surname: str) -> bool:
    s = surname.lower()
    return any((leg.player or "").lower() == s or (leg.team and False)
               for leg in scenario.legs)


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    inact = news.get_inactives(ctx.settings.game)

    voids: dict[str, list[str]] = {}
    lines = [f"[lineups] {ctx.settings.game_id}  source={inact.source} "
             f"confirmed={inact.confirmed}"]

    if not inact.confirmed:
        lines.append("  inactives UNCONFIRMED — wire news.get_inactives or set "
                     "sources.known_inactives in game.yaml. Treating all scenarios live.")
    else:
        out_surnames = [n.split()[-1].lower() for n in inact.out]
        for sc in ctx.scenarios:
            hit = [sn for sn in out_surnames if _player_in_scenario(sc, sn)]
            if hit:
                voids[sc.id] = hit
        if inact.out:
            lines.append(f"  OUT: {', '.join(inact.out)}")
        if inact.questionable:
            lines.append(f"  GTD: {', '.join(inact.questionable)}")
        for sid, who in voids.items():
            lines.append(f"  VOID {sid} — depends on {', '.join(who)}")
        if not voids:
            lines.append("  no tracked-player scratches affect any scenario")

    payload = {
        "game_id": ctx.settings.game_id,
        "phase": "lineups",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "confirmed": inact.confirmed,
        "out": inact.out,
        "questionable": inact.questionable,
        "void_scenarios": voids,
    }
    ctx.write_json("lineups.json", payload)
    deliver("\n".join(lines), ctx.settings.deliver_to)
    return payload
