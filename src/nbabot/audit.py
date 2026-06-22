"""Append-only audit and dead-letter logging for research/execution actions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .research import ResearchStore, utc_now


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


class AuditTrail:
    def __init__(self, data_dir: Path, store: ResearchStore | None = None):
        self.data_dir = data_dir
        self.store = store

    def log(self, event_type: str, payload: dict[str, Any],
            game_id: str | None = None) -> None:
        record = {"ts": utc_now(), "type": event_type, "game_id": game_id, **payload}
        _append_jsonl(self.data_dir / "audit.jsonl", record)
        if self.store:
            self.store.record_audit(event_type, record, game_id)

    def dead_letter(self, event_type: str, error: str, payload: dict[str, Any],
                    game_id: str | None = None) -> None:
        record = {
            "ts": utc_now(),
            "type": event_type,
            "game_id": game_id,
            "error": error,
            "payload": payload,
        }
        _append_jsonl(self.data_dir / "dlq.jsonl", record)
        if self.store:
            self.store.record_dlq(event_type, error, payload, game_id)
