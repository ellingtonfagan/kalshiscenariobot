"""Phase: ui. Serve the local browser dashboard."""
from __future__ import annotations

from ..ui import serve
from .base import Context, load_context


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    serve(ctx)
    return {"reason": "stopped"}
