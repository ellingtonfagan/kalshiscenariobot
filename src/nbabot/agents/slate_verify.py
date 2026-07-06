"""Phase: slate-verify. Verify slate-discovery findings before research."""
from __future__ import annotations

from .. import guardrails
from ..alerts import deliver
from ..odds_refresh import artifact_freshness, refresh_if_stale
from ..slate import verify_slate
from .base import Context, load_context


def _format(payload: dict) -> str:
    return (
        f"[slate-verify] {payload['game_id']}: verified={payload.get('verified_count', 0)}/"
        f"{payload.get('candidate_count', 0)} execution_ready={payload.get('execution_ready_count', 0)}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    from . import slate_discovery

    live_updates = {
        "slate_candidates": refresh_if_stale(
            ctx,
            "slate_candidates.json",
            slate_discovery.run,
        )
    }
    slate = ctx.read_json("slate_candidates.json") or {"candidates": []}
    payload = verify_slate(ctx, slate)
    payload["live_updates"] = live_updates
    payload["input_freshness"] = artifact_freshness(ctx, ("slate_candidates.json",))
    ctx.write_json("slate_verification.json", payload)
    deliver(guardrails.with_footer(_format(payload)), ctx.settings.deliver_to)
    return payload
