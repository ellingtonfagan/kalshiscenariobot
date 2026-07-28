"""Phase: qual-index. Incrementally build retrieval corpus for qual research."""
from __future__ import annotations

from ..alerts import deliver
from ..research import ResearchStore
from .base import Context, load_context


def _format(payload: dict) -> str:
    counts = payload.get("source_counts") or {}
    count_text = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "empty"
    return (
        f"[qual-index] status={payload.get('status')} "
        f"chunks={payload.get('chunk_count', 0)} written={payload.get('written', 0)} "
        f"sources={count_text}"
    )


def run(ctx: Context | None = None) -> dict:
    ctx = ctx or load_context()
    store = ResearchStore(ctx.settings.research_db_path)
    payload = store.build_qual_rag_index()
    payload["corpus"] = store.qual_rag_corpus_summary()
    ctx.write_json("qual_index.json", payload)
    deliver(_format(payload), ctx.settings.deliver_to)
    return payload
