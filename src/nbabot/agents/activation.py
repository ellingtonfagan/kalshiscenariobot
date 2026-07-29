"""Helpers for audited agent-to-agent activation chains."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..audit import AuditTrail

if TYPE_CHECKING:
    from .base import Context

StepFn = Callable[["Context"], dict]


def run_activated_step(
    ctx: "Context",
    name: str,
    fn: StepFn,
    steps: list[dict[str, Any]],
    audit: AuditTrail,
    *,
    activated_by: str | None,
    activates: list[str] | tuple[str, ...] = (),
    dlq_type: str,
) -> dict | None:
    """Run one step and annotate which previous agent activated it."""
    try:
        result = fn(ctx)
    except Exception as e:
        audit.dead_letter(dlq_type, str(e), {"step": name, "activated_by": activated_by}, ctx.settings.game_id)
        steps.append({
            "name": name,
            "status": "error",
            "activated_by": activated_by,
            "activates": [],
            "error": str(e),
        })
        return None
    steps.append({
        "name": name,
        "status": "ok",
        "activated_by": activated_by,
        "activates": list(activates),
        "result": result,
    })
    return result


def skip_activated_step(
    name: str,
    steps: list[dict[str, Any]],
    *,
    activated_by: str | None,
    reason: str,
    activates: list[str] | tuple[str, ...] = (),
) -> None:
    """Record an intentional skip in the activation chain."""
    steps.append({
        "name": name,
        "status": "skipped",
        "activated_by": activated_by,
        "activates": list(activates),
        "reason": reason,
    })


def activation_edges(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return a compact graph view of completed/skipped/error activations."""
    edges = []
    for step in steps:
        if step.get("activated_by") is not None:
            edges.append({
                "from": str(step["activated_by"]),
                "to": str(step["name"]),
                "status": str(step["status"]),
            })
    return edges
