"""Phase: heartbeat (tip→buzzer). ONE live tick of HEARTBEAT.md.

Skip early if not live. Pull box score + prices, evaluate each scenario, run §5 triggers,
and emit a compact block ONLY when something changed since the last tick. On final, set
buzzer_detected and signal reconcile.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import scores, triggers
from ..alerts import deliver, format_block
from ..scenarios import ScenarioState, evaluate
from .base import Context, load_context, resolve_event_id


def _state_signature(scen_states: list[ScenarioState]) -> dict[str, str]:
    return {ss.id: ss.state for ss in scen_states}


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    event_id = resolve_event_id(ctx)
    if not event_id:
        deliver("[heartbeat] skip: no ESPN event id (pre-listing?)", ctx.settings.deliver_to)
        return {"reason": "no-event"}

    gs = scores.get_game_state(event_id)

    # Skip conditions ----------------------------------------------------------------
    if gs.state == "pre":
        return {"reason": "not-live"}
    if gs.state == "post":
        ctx.write_json("buzzer.json", {
            "event_id": event_id, "detected_at": datetime.now(timezone.utc).isoformat(),
            "status": gs.status_detail})
        deliver(f"[heartbeat] FINAL detected ({gs.status_detail}). Run `nbabot reconcile`.",
                ctx.settings.deliver_to)
        return {"reason": "final", "event_id": event_id}

    voids = set((ctx.read_json("lineups.json") or {}).get("void_scenarios", {}))
    props = ctx.kalshi.prop_prices(ctx.game_tag)
    winners = ctx.kalshi.winner_prices(ctx.game_tag)

    halftime_total = gs.total if gs.period == 2 else None
    trigger_hits = triggers.evaluate(gs, halftime_total)
    override = {t.scenario_id: t.override_state for t in trigger_hits if t.override_state}

    scen_states: list[ScenarioState] = []
    for sc in ctx.scenarios:
        if sc.id in voids:
            scen_states.append(ScenarioState(sc.id, "VOID", [], None, 0, len(sc.legs),
                                             note="key player out"))
            continue
        hc = float(ctx.haircut.get(sc.id, 1.0))
        ss = evaluate(sc, props, winners, gs, hc)
        if sc.id in override and ss.state not in ("DEAD",):
            ss.state = override[sc.id]
            ss.note = (ss.note + "; trigger override").strip("; ")
        scen_states.append(ss)

    # Emit only on change -------------------------------------------------------------
    prev = (ctx.read_json("hb_state.json") or {}).get("signature", {})
    sig = _state_signature(scen_states)
    changed = sig != prev or bool(trigger_hits)

    ctx.write_json("hb_state.json", {
        "tick_at": datetime.now(timezone.utc).isoformat(),
        "period": gs.period, "clock": gs.clock, "signature": sig,
    })

    if not changed:
        return {"reason": "no-change", "signature": sig}

    header = (f"[Q{gs.period} {gs.clock}] {ctx.settings.game_id}  "
              f"{gs.away_abbr} {gs.away_score}-{gs.home_score} {gs.home_abbr}")
    block = format_block(header, scen_states, trigger_hits)
    deliver(block, ctx.settings.deliver_to)
    return {"reason": "emitted", "signature": sig, "triggers": [t.message for t in trigger_hits]}
