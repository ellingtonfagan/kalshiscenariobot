"""Phase: reconcile (T+30m). Resolve legs, append the learning log, recalibrate.

This is the only step that writes outcomes + updates calibration — it's what makes the
bot improve. Writes a 5-line recap and the updated sgp_haircut suggestions.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from .. import calibration, scores
from ..alerts import deliver
from ..calibration import LegResult, LogEntry, ScenarioResult
from ..research import ResearchStore
from ..scenarios import price_leg, resolve_leg
from .base import Context, load_context, resolve_event_id


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    event_id = resolve_event_id(ctx)
    gs = scores.get_game_state(event_id) if event_id else scores.GameState()

    locked = (ctx.read_json("locked_board.json") or {}).get("legs", {})
    voids = set((ctx.read_json("lineups.json") or {}).get("void_scenarios", {}))

    # current prices only used to backfill entry prob if lock didn't capture it
    props = ctx.kalshi.prop_prices(ctx.game_tag) if gs.is_final else {}
    winners = ctx.kalshi.winner_prices(ctx.game_tag) if gs.is_final else {}

    entry = LogEntry(
        game_id=ctx.settings.game_id,
        logged_at=datetime.now(timezone.utc).isoformat(),
    )
    recap = [f"[reconcile] {ctx.settings.game_id}  final={gs.is_final} "
             f"score={gs.away_abbr} {gs.away_score}-{gs.home_score} {gs.home_abbr}"]
    research_legs = []

    for sc in ctx.scenarios:
        if sc.id in voids:
            recap.append(f"  {sc.id} VOID (key player out)")
            continue
        locked_legs = {r["market"]: r for r in locked.get(sc.id, [])}
        joint_prior = 1.0
        leg_outcomes = []
        for leg in sc.legs:
            outcome = resolve_leg(leg, gs)
            entry_p = locked_legs.get(leg.market, {}).get("locked_implied_p")
            if entry_p is None:
                entry_p = price_leg(leg, props, winners)
            entry.legs.append(LegResult(
                market=leg.market, line=leg.line, prior_p=leg.prior_p,
                entry_implied_p=entry_p, outcome=outcome))
            research_legs.append({
                "scenario_id": sc.id,
                "market": leg.market,
                "line": leg.line,
                "prior_p": leg.prior_p,
                "entry_implied_p": entry_p,
                "outcome": outcome,
            })
            if outcome is not None:
                joint_prior *= leg.prior_p
            leg_outcomes.append(outcome)

        resolvable = [o for o in leg_outcomes if o is not None]
        hit = int(bool(resolvable) and all(o == 1 for o in resolvable))
        prior_p_joint = round(joint_prior, 5) if resolvable else None
        entry.scenarios.append(ScenarioResult(
            id=sc.id, prior_p_joint=prior_p_joint, hit=hit,
            notes=f"{sum(o==1 for o in resolvable)}/{len(resolvable)} resolvable legs hit",
            resolved_legs=len(resolvable), total_legs=len(sc.legs)))
        prior_label = f"{prior_p_joint:.3f}" if prior_p_joint is not None else "n/a"
        recap.append(f"  {sc.id} {'HIT' if hit else 'miss'} "
                     f"({sum(o==1 for o in resolvable)}/{len(resolvable)} legs, "
                     f"prior joint {prior_label})")

    log_path = ctx.settings.data_path("log.jsonl")
    calibration.append_log(log_path, entry)
    summary = calibration.recompute(log_path)
    overrides = calibration.suggest_overrides(summary, ctx.settings.game_id)
    override_path = getattr(
        ctx.settings,
        "calibration_overrides_path",
        ctx.settings.data_path("calibration_overrides.json"),
    )
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text(json.dumps(overrides, indent=2, default=str))
    if hasattr(ctx.settings, "research_db_path"):
        ResearchStore(ctx.settings.research_db_path).record_scenario_results(
            ctx.settings.game_id,
            [asdict(sc) for sc in entry.scenarios],
            research_legs,
        )

    recap.append(f"  calibration: {summary.get('families', {})}")
    recap.append(f"  suggested sgp_haircut: {summary.get('haircut', {})}")
    ctx.write_json("reconcile.json", {
        "entry": asdict(entry),
        "summary": summary,
        "calibration_overrides_path": str(override_path),
        "calibration_overrides": overrides,
    })
    deliver("\n".join(recap[:8]), ctx.settings.deliver_to)
    return {"summary": summary, "logged": str(log_path)}
