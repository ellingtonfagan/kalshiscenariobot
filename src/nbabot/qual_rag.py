"""Retrieval and groundedness helpers for qualitative scenario research."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

EMBEDDING_VERSION = "hashbow-v1"
EMBEDDING_DIM = 96
HYBRID_RRF_K = 30
DEFAULT_CONTEXT_LIMIT = 8
MIN_CHUNK_CHARS = 80
MAX_CHUNK_CHARS = 1200
SUPPORTED_BRANCH_STATUSES = {"supported", "unsupported"}
EVIDENCE_SOURCE_TYPES = {"news_items", "settlement_records", "qual_postmortems", "qual_lessons"}
OPINION_SOURCE_TYPES = {"qual_signals"}
MIN_EVIDENCE_CONTEXTS = 2
MIN_EVIDENCE_SHARE = 0.50
MAX_OPINION_CONTEXTS = 2

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]*", re.I)


@dataclass(frozen=True)
class RetrievalQuery:
    ticker: str
    text: str
    teams: tuple[str, ...] = ()
    market_family: str = ""
    close_time: str | None = None
    limit: int = DEFAULT_CONTEXT_LIMIT


def parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def timestamp_lte(left: Any, right: Any) -> bool:
    """Return True when ``left`` is not after ``right``.

    Missing timestamps are deliberately conservative for no-lookahead checks:
    undated chunks are not retrievable for a market with a known close time.
    """
    if not right:
        return True
    left_dt = parse_time(left)
    right_dt = parse_time(right)
    return bool(left_dt is not None and right_dt is not None and left_dt <= right_dt)


def tokens(text: Any) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(str(text or ""))]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding(text: Any, *, dim: int = EMBEDDING_DIM) -> list[float]:
    """Small deterministic local embedding.

    This is a hashed bag-of-terms vector, normalized for cosine scoring. It is
    intentionally stdlib-only: the corpus is thousands of chunks, not millions.
    """
    vec = [0.0] * dim
    counts = Counter(tokens(text))
    if not counts:
        return vec
    for tok, count in counts.items():
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return [0.0] * dim
    return [round(v / norm, 8) for v in vec]


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def keyword_score(query_text: str, chunk: dict[str, Any]) -> float:
    query_counts = Counter(tokens(query_text))
    if not query_counts:
        return 0.0
    fields = " ".join(
        str(chunk.get(key) or "")
        for key in ("text", "teams_json", "market_family", "source_type", "ticker")
    )
    doc_counts = Counter(tokens(fields))
    if not doc_counts:
        return 0.0
    score = 0.0
    doc_len = sum(doc_counts.values()) or 1
    avg_len = 80.0
    k1 = 1.2
    b = 0.75
    for term, qtf in query_counts.items():
        tf = doc_counts.get(term, 0)
        if tf <= 0:
            continue
        # Single-corpus BM25-ish term saturation. Exact team/player/market terms
        # are more important here than global corpus IDF.
        denom = tf + k1 * (1 - b + b * doc_len / avg_len)
        score += qtf * ((tf * (k1 + 1)) / denom)
    return round(score, 6)


def rrf_fuse(
    dense_ranked: list[dict[str, Any]],
    sparse_ranked: list[dict[str, Any]],
    *,
    k: int = HYBRID_RRF_K,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for rows, score_key in ((dense_ranked, "dense_score"), (sparse_ranked, "sparse_score")):
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row.get("chunk_id"))
            by_id.setdefault(chunk_id, dict(row))
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            by_id[chunk_id][score_key] = row.get(score_key, row.get("score"))
    out = []
    for chunk_id, row in by_id.items():
        row["hybrid_score"] = round(scores[chunk_id], 8)
        out.append(row)
    return sorted(out, key=lambda row: row.get("hybrid_score") or 0.0, reverse=True)


def rerank(query: RetrievalQuery, rows: list[dict[str, Any]], *, limit: int | None = None) -> list[dict[str, Any]]:
    query_terms = set(tokens(" ".join([query.text, query.market_family, " ".join(query.teams)])))
    ranked = []
    for row in rows:
        chunk_terms = set(tokens(row.get("text")))
        overlap = len(query_terms & chunk_terms)
        team_bonus = 0.0
        teams = _json_list(row.get("teams_json"))
        if query.teams and any(team in teams for team in query.teams):
            team_bonus = 0.15
        family_bonus = 0.10 if query.market_family and query.market_family == row.get("market_family") else 0.0
        source_bonus = {
            "qual_postmortems": 0.08,
            "qual_lessons": 0.07,
            "settlement_records": 0.06,
            "qual_signals": -0.04,
            "closing_snapshots": 0.03,
            "news_items": 0.05,
        }.get(str(row.get("source_type") or ""), 0.0)
        final = (
            float(row.get("hybrid_score") or 0.0)
            + 0.025 * overlap
            + team_bonus
            + family_bonus
            + source_bonus
        )
        item = dict(row)
        item["rerank_score"] = round(final, 8)
        item["lexical_overlap"] = overlap
        ranked.append(item)
    ranked.sort(key=lambda row: row.get("rerank_score") or 0.0, reverse=True)
    return ranked[: (limit or query.limit)]


def is_evidence_context(row: dict[str, Any]) -> bool:
    return str(row.get("source_type") or "") in EVIDENCE_SOURCE_TYPES


def is_opinion_context(row: dict[str, Any]) -> bool:
    return str(row.get("source_type") or "") in OPINION_SOURCE_TYPES


def compose_contexts(rows: list[dict[str, Any]], *, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enforce evidence composition instead of relying on ranking preference."""
    limit = max(int(limit), 0)
    if limit <= 0:
        return [], {
            "source_counts": {},
            "evidence_count": 0,
            "opinion_count": 0,
            "evidence_share": 0.0,
            "evidence_floor_met": False,
        }
    min_evidence = min(limit, max(MIN_EVIDENCE_CONTEXTS, int(math.ceil(limit * MIN_EVIDENCE_SHARE))))
    evidence = [row for row in rows if is_evidence_context(row)]
    opinion = [row for row in rows if is_opinion_context(row)]
    other = [row for row in rows if not is_evidence_context(row) and not is_opinion_context(row)]
    selected = evidence[:min_evidence]
    remaining = limit - len(selected)
    opinion_cap = min(MAX_OPINION_CONTEXTS, max(remaining, 0))
    selected.extend(opinion[:opinion_cap])
    remaining = limit - len(selected)
    selected.extend(other[:remaining])
    remaining = limit - len(selected)
    if remaining > 0:
        selected.extend(evidence[min_evidence:min_evidence + remaining])
    seen = set()
    deduped = []
    for row in selected:
        chunk_id = str(row.get("chunk_id") or row.get("evidence_id") or "")
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        item = dict(row)
        item["context_role"] = "evidence" if is_evidence_context(item) else "opinion" if is_opinion_context(item) else "context"
        deduped.append(item)
    source_counts = dict(Counter(str(row.get("source_type") or "unknown") for row in deduped))
    evidence_count = sum(1 for row in deduped if is_evidence_context(row))
    opinion_count = sum(1 for row in deduped if is_opinion_context(row))
    evidence_share = round(evidence_count / len(deduped), 6) if deduped else 0.0
    return deduped, {
        "source_counts": source_counts,
        "evidence_count": evidence_count,
        "opinion_count": opinion_count,
        "evidence_share": evidence_share,
        "evidence_floor_met": bool(evidence_count >= min_evidence),
        "min_evidence_contexts": min_evidence,
        "max_opinion_contexts": MAX_OPINION_CONTEXTS,
    }


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(v) for v in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def chunk_text_by_paragraphs(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z0-9])", str(text or "")) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            for start in range(0, len(para), max_chars):
                chunks.append(para[start:start + max_chars].strip())
            continue
        candidate = f"{current} {para}".strip() if current else para
        if len(candidate) > max_chars and current:
            chunks.append(current.strip())
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if len(chunk) >= MIN_CHUNK_CHARS] or ([str(text or "").strip()] if str(text or "").strip() else [])


def branch_tradeable_mass(branches: Iterable[dict[str, Any]]) -> float:
    mass = 0.0
    for branch in branches:
        if str(branch.get("status") or "supported") != "supported":
            continue
        try:
            mass += float(branch.get("p_event") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(mass, 6)


def supported_probability(branches: Iterable[dict[str, Any]]) -> float:
    prob = 0.0
    for branch in branches:
        if str(branch.get("status") or "supported") != "supported":
            continue
        try:
            prob += float(branch.get("p_event") or 0.0) * float(branch.get("p_outcome_given_event") or 0.0)
        except (TypeError, ValueError):
            continue
    return round(prob, 6)


def validate_branch_evidence(branches: Iterable[dict[str, Any]], allowed_evidence_ids: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    out = []
    errors = []
    for idx, raw in enumerate(branches):
        branch = dict(raw)
        evidence_ids = [
            str(eid)
            for eid in (branch.get("evidence_ids") or [])
            if str(eid) in allowed_evidence_ids
        ]
        status = str(branch.get("status") or ("supported" if evidence_ids else "unsupported"))
        if status not in SUPPORTED_BRANCH_STATUSES:
            errors.append(f"branch {idx} invalid status")
            status = "unsupported"
        if status == "supported" and not evidence_ids:
            errors.append(f"branch {idx} supported without evidence")
            status = "unsupported"
        branch["status"] = status
        branch["evidence_ids"] = sorted(dict.fromkeys(evidence_ids))
        out.append(branch)
    return out, errors


def groundedness_score(signal: dict[str, Any], contexts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    all_context_rows = list(contexts)
    context_rows = [row for row in all_context_rows if is_evidence_context(row)]
    by_id = {str(row.get("evidence_id") or row.get("chunk_id")): row for row in context_rows}
    context_text_by_id = {
        eid: " ".join(tokens(row.get("text")))
        for eid, row in by_id.items()
    }
    total = 0
    supported = 0
    unsupported = 0
    relevance_hits = 0
    for branch in signal.get("scenarios") or []:
        total += 1
        evidence_ids = [str(eid) for eid in branch.get("evidence_ids") or []]
        if str(branch.get("status") or "") != "supported" or not evidence_ids:
            unsupported += 1
            continue
        branch_terms = set(tokens(branch.get("event")))
        if not branch_terms:
            unsupported += 1
            continue
        matched = False
        for eid in evidence_ids:
            ctx_terms = set(context_text_by_id.get(eid, "").split())
            if len(branch_terms & ctx_terms) >= max(1, min(3, len(branch_terms))):
                matched = True
                break
        if matched:
            supported += 1
        else:
            unsupported += 1
    query_terms = set(tokens(" ".join([
        str(signal.get("ticker") or ""),
        str((signal.get("market") or {}).get("title") or ""),
        str((signal.get("market") or {}).get("market_family") or ""),
    ])))
    for row in context_rows:
        if query_terms and query_terms & set(tokens(row.get("text"))):
            relevance_hits += 1
    faithfulness = supported / total if total else 0.0
    context_relevance = relevance_hits / len(context_rows) if context_rows else 0.0
    unsupported_rate = unsupported / total if total else 1.0
    composite = 0.7 * faithfulness + 0.3 * context_relevance
    return {
        "faithfulness": round(faithfulness, 6),
        "context_relevance": round(context_relevance, 6),
        "groundedness": round(composite, 6),
        "unsupported_branch_rate": round(unsupported_rate, 6),
        "supported_branch_count": supported,
        "branch_count": total,
        "evidence_context_count": len(context_rows),
        "opinion_context_count": sum(1 for row in all_context_rows if is_opinion_context(row)),
        "evidence_source_types": sorted({str(row.get("source_type") or "") for row in context_rows}),
    }
