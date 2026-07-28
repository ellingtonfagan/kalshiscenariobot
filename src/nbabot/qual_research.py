"""LLM-backed qualitative probabilities for Kalshi markets lacking consensus."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .performance_learner import market_family
from .qual_rag import (
    RetrievalQuery,
    branch_tradeable_mass,
    compose_contexts,
    cosine,
    embedding,
    groundedness_score,
    is_evidence_context,
    keyword_score,
    rerank,
    rrf_fuse,
    supported_probability,
    timestamp_lte,
    validate_branch_evidence,
)
from .research import utc_now
from .research_news import ResearchTeam, item_matches_team


MIN_CONFIDENCE = 0.55
USAGE_LIMIT_RE = re.compile(r"(usage limit|quota|rate limit|limit reached|too many requests)", re.I)
QUAL_PROMPT_VERSION = "qual-scenario-rag-v2"
SCENARIO_EVENT_SUM_TOLERANCE = 0.025
SCENARIO_PROB_SUM_TOLERANCE = 0.015
MIN_TRADEABLE_BRANCH_MASS = 0.50
PRIMARY_UNAVAILABLE_REASONS = {"usage-limit", "timeout", "command not found", "missing command"}


QUAL_SIGNAL_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ticker",
            "base_rate",
            "qual_prob",
            "confidence",
            "rationale",
            "scenarios",
            "citation_urls",
            "news_item_ids_used",
        ],
        "properties": {
            "ticker": {"type": "string"},
            "base_rate": {"type": "number"},
            "qual_prob": {"type": "number"},
            "confidence": {"type": "number"},
            "rationale": {"type": "string"},
            "scenarios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["event", "p_event", "p_outcome_given_event", "citations", "evidence_ids", "status"],
                    "properties": {
                        "event": {"type": "string"},
                        "p_event": {"type": "number"},
                        "p_outcome_given_event": {"type": "number"},
                        "citations": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "status": {"type": "string", "enum": ["supported", "unsupported"]},
                    },
                },
            },
            "citation_urls": {"type": "array", "items": {"type": "string"}},
            "news_item_ids_used": {"type": "array", "items": {"type": "string"}},
        },
    },
}

NEAR_MISS_INVESTIGATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "justified",
        "direction_consistent",
        "confidence",
        "rationale",
        "citation_urls",
    ],
    "properties": {
        "justified": {"type": "boolean"},
        "direction_consistent": {"type": "boolean"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "citation_urls": {"type": "array", "items": {"type": "string"}},
        "news_item_ids_used": {"type": "array", "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class QualValidationResult:
    accepted: list[dict[str, Any]]
    discarded: list[dict[str, Any]]
    retry_error: str | None = None


def _safe_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return value


def _clamp_prob(value: float) -> float:
    return round(min(max(value, 0.02), 0.98), 6)


def _prob_or_none(raw: Any) -> float | None:
    value = _safe_float(raw)
    if value is None or value < 0 or value > 1:
        return None
    return round(value, 6)


def _team_lookup(teams: list[ResearchTeam]) -> dict[str, ResearchTeam]:
    return {team.key: team for team in teams}


def _matches_any_team(text: str, teams: list[ResearchTeam]) -> list[str]:
    lower = text.lower()
    out = []
    for team in teams:
        if any(str(alias).lower() in lower for alias in team.aliases if alias):
            out.append(team.key)
    return out


def _candidate_text(candidate: dict[str, Any], market: dict[str, Any]) -> str:
    pieces = [
        candidate.get("candidate_id"),
        candidate.get("away_team"),
        candidate.get("home_team"),
        candidate.get("team"),
        market.get("title"),
        market.get("yes_sub_title"),
        market.get("no_sub_title"),
        market.get("rules_primary"),
        market.get("rules_secondary"),
    ]
    pieces.extend(str(item) for item in market.get("components") or [])
    return " ".join(str(piece) for piece in pieces if piece)


def unpriced_team_market_rows(
    *,
    teams: list[ResearchTeam],
    slate: dict[str, Any],
    market_matches: dict[str, Any],
    limit: int = 80,
) -> list[dict[str, Any]]:
    candidates = {
        candidate.get("candidate_id"): candidate
        for candidate in slate.get("candidates") or []
        if isinstance(candidate, dict) and candidate.get("candidate_id")
    }
    rows = []
    for row in market_matches.get("rows") or []:
        if not isinstance(row, dict) or not row.get("ticker"):
            continue
        candidate = candidates.get(row.get("candidate_id")) or {}
        if candidate.get("line_markets"):
            continue
        text = _candidate_text(candidate, row)
        team_keys = _matches_any_team(text, teams)
        if not team_keys:
            continue
        quote = row.get("kalshi_quote") if isinstance(row.get("kalshi_quote"), dict) else {}
        orderbook = row.get("orderbook") if isinstance(row.get("orderbook"), dict) else {}
        yes_book = orderbook.get("yes") if isinstance(orderbook.get("yes"), dict) else {}
        price = yes_book.get("vwap_cents") or yes_book.get("best_ask_cents") or quote.get("ask") or quote.get("mid")
        rows.append({
            "ticker": row.get("ticker"),
            "candidate_id": row.get("candidate_id"),
            "teams": team_keys,
            "market_family": market_family({
                "ticker": row.get("ticker"),
                "title": row.get("title"),
                "market": row.get("title"),
                "market_identity": row.get("market_identity"),
            }),
            "title": row.get("title"),
            "close_time": row.get("close_time"),
            "kalshi_price_cents": price,
            "bid_cents": yes_book.get("best_bid_cents") or quote.get("bid"),
            "ask_cents": yes_book.get("best_ask_cents") or quote.get("ask"),
        })
        if len(rows) >= limit:
            break
    return rows


def news_for_prompt(news_items: list[dict[str, Any]], teams: list[ResearchTeam], *, max_items: int = 60) -> list[dict[str, Any]]:
    by_key = _team_lookup(teams)
    rows = []
    for item in news_items:
        team = by_key.get(str(item.get("team") or ""))
        if team is None:
            continue
        if not item.get("url"):
            continue
        # Shared league/community feeds can leak irrelevant rows; keep the prompt team-scoped.
        if not item_matches_team(item, team):
            source = str(item.get("source") or "")
            if source.startswith("espn_") or source == "espn_mlb" or source.startswith("reddit_baseball"):
                continue
        rows.append({
            "id": item.get("id"),
            "team": team.key,
            "source": item.get("source"),
            "title": item.get("title"),
            "summary": str(item.get("body") or "")[:900],
            "url": item.get("url"),
            "published_at": item.get("published_at"),
        })
    rows.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
    return rows[:max_items]


def build_retrieval_query(market: dict[str, Any]) -> RetrievalQuery:
    text = " ".join(
        str(market.get(key) or "")
        for key in ("ticker", "title", "candidate_id", "market_family")
    )
    text = " ".join([
        text,
        " ".join(str(team) for team in market.get("teams") or []),
    ]).strip()
    return RetrievalQuery(
        ticker=str(market.get("ticker") or ""),
        text=text,
        teams=tuple(str(team) for team in (market.get("teams") or []) if str(team)),
        market_family=str(market.get("market_family") or ""),
        close_time=market.get("close_time"),
    )


def retrieve_contexts_for_market(
    *,
    market: dict[str, Any],
    chunks: list[dict[str, Any]],
    limit: int = 8,
) -> dict[str, Any]:
    """Hybrid retrieval with explicit no-lookahead filtering.

    Dense similarity gives semantic-ish recall from the local hashed vectors.
    Sparse scoring keeps exact teams, players, tickers, and market families
    from being washed out. Reranking then rewards source precedent and direct
    team/family overlap.
    """
    query = build_retrieval_query(market)
    q_vec = embedding(query.text)
    eligible = [
        chunk for chunk in chunks
        if timestamp_lte(chunk.get("source_timestamp"), query.close_time)
    ]
    leaked = len(chunks) - len(eligible) if query.close_time else 0
    if not eligible:
        return {"query": query, "contexts": [], "leaked_context_count": leaked}
    dense = []
    sparse = []
    for chunk in eligible:
        c_vec = chunk.get("embedding") or []
        dense_score = cosine(q_vec, c_vec) if c_vec else 0.0
        sparse_score = keyword_score(query.text, chunk)
        base = {
            "chunk_id": chunk.get("chunk_id"),
            "evidence_id": chunk.get("chunk_id"),
            "source_type": chunk.get("source_type"),
            "source_id": chunk.get("source_id"),
            "source_row_id": chunk.get("source_row_id"),
            "source_timestamp": chunk.get("source_timestamp"),
            "team": chunk.get("team"),
            "teams": chunk.get("teams") or [],
            "teams_json": json.dumps(chunk.get("teams") or [], sort_keys=True),
            "ticker": chunk.get("ticker"),
            "market_family": chunk.get("market_family"),
            "title": chunk.get("title"),
            "text": chunk.get("text"),
            "metadata": chunk.get("metadata") or {},
        }
        dense.append({**base, "dense_score": round(dense_score, 8), "score": dense_score})
        sparse.append({**base, "sparse_score": sparse_score, "score": sparse_score})
    dense.sort(key=lambda row: row.get("dense_score") or 0.0, reverse=True)
    sparse.sort(key=lambda row: row.get("sparse_score") or 0.0, reverse=True)
    fused = rrf_fuse(dense[: max(limit * 8, 40)], sparse[: max(limit * 8, 40)])
    reranked = rerank(query, fused, limit=max(limit * 4, 24))
    contexts, composition = compose_contexts(reranked, limit=limit)
    return {
        "query": query,
        "contexts": contexts,
        "leaked_context_count": leaked,
        "composition": composition,
    }


def build_prompt(
    *,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    retrieval_contexts: dict[str, list[dict[str, Any]]] | None = None,
    lessons: list[dict[str, Any]] | None = None,
    calibration_lines: list[str] | None = None,
    retry_error: str | None = None,
) -> str:
    schema = (
        '[{"ticker":"...","base_rate":0.50,"qual_prob":0.50,"confidence":0.70,'
        '"rationale":"One or two sentences.",'
        '"scenarios":[{"event":"short branch description","p_event":0.50,'
        '"p_outcome_given_event":0.60,"citations":["https://..."],'
        '"evidence_ids":["chunk-id"],"status":"supported"}],'
        '"citation_urls":["https://..."],"news_item_ids_used":["..."]}]'
    )
    retrieval_contexts = retrieval_contexts or {}
    allowed_evidence_ids = sorted({
        str(row.get("evidence_id") or row.get("chunk_id"))
        for rows in retrieval_contexts.values()
        for row in rows
        if row.get("context_role") == "evidence" or is_evidence_context(row)
        if row.get("evidence_id") or row.get("chunk_id")
    })
    payload = {
        "news_items": news_items,
        "markets": markets,
        "retrieved_context_by_ticker": retrieval_contexts,
        "allowed_tickers": [row["ticker"] for row in markets],
        "allowed_citation_urls": sorted({row["url"] for row in news_items if row.get("url")}),
        "allowed_news_item_ids": sorted({row["id"] for row in news_items if row.get("id")}),
        "allowed_evidence_ids": allowed_evidence_ids,
        "lessons": lessons or [],
        "calibration": calibration_lines or [],
        "prompt_version": QUAL_PROMPT_VERSION,
    }
    retry_line = f"\nPrevious output was invalid: {retry_error}\n" if retry_error else ""
    return (
        "You are pricing Kalshi sports markets that lack sportsbook consensus. "
        "Use only the provided news/discussion items and market rows. "
        "Prefer retrieved_context_by_ticker over raw recency. "
        "Prior qual_signals are opinion context only: they may inform priors, but they are not evidence. "
        "Supported scenario evidence_ids must come from external or outcome-bearing contexts: news_items, settlement_records, qual_postmortems, or qual_lessons. "
        "Decompose every forecast into 2-4 mutually exclusive, collectively exhaustive scenario branches. "
        "The scenario p_event values must sum to about 1.0, and qual_prob must equal "
        "sum(p_event * p_outcome_given_event) within rounding. "
        "Every scenario branch must include evidence_ids copied from allowed_evidence_ids. "
        "If a branch is plausible but not supported by retrieved evidence, set status='unsupported'; "
        "unsupported branches are for analysis only and cannot justify tradeable probability. "
        "base_rate is your prior before the provided news; qual_prob is after applying the news. "
        "Use the lessons and calibration lines to correct recurring bias without copying them blindly. "
        "Return STRICT JSON only, with no markdown, no prose wrapper, and no comments. "
        "Every citation_url must be copied from allowed_citation_urls. "
        "Every scenario citation must also be copied from allowed_citation_urls. "
        "Do not invent tickers or citations. "
        "If evidence is too thin for a market, omit it. "
        "Use probabilities for YES contracts.\n"
        f"Schema: {schema}\n"
        f"{retry_line}"
        f"Input JSON:\n{json.dumps(payload, sort_keys=True)}"
    )


def _command_args(command: str) -> list[str]:
    args = shlex.split(os.path.expanduser(command))
    if args:
        args[0] = os.path.expanduser(args[0])
    return args


def invoke_codex_cli(command: str, prompt: str, *, timeout_seconds: int) -> tuple[bool, str, str]:
    args = _command_args(command)
    if not args:
        return False, "", "missing command"
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return False, "", "command not found"
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except Exception as exc:
        return False, "", exc.__class__.__name__
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if USAGE_LIMIT_RE.search(combined):
        return False, combined, "usage-limit"
    if proc.returncode != 0:
        return False, combined, f"exit-{proc.returncode}"
    return True, proc.stdout, ""


def _primary_unavailable_reason(reason: str) -> bool:
    reason = str(reason or "")
    return reason in PRIMARY_UNAVAILABLE_REASONS or reason.startswith("timeout")


def _anthropic_output_text(message: Any) -> str:
    direct = getattr(message, "output_text", None)
    if direct:
        return str(direct)
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict):
            if block.get("text") is not None:
                parts.append(str(block["text"]))
            elif block.get("input") is not None:
                return json.dumps(block["input"], sort_keys=True)
            continue
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        block_input = getattr(block, "input", None)
        if block_input is not None:
            return json.dumps(block_input, sort_keys=True)
    return "\n".join(parts).strip()


def invoke_claude_api(
    prompt: str,
    *,
    schema: dict[str, Any],
    model: str,
    client_factory: Any | None = None,
) -> tuple[bool, str, str]:
    try:
        import anthropic
    except Exception:
        return False, "", "anthropic-sdk-unavailable"
    try:
        client = client_factory() if client_factory is not None else anthropic.Anthropic()
        message = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        )
        output = _anthropic_output_text(message)
        return True, output, ""
    except (
        getattr(anthropic, "RateLimitError", Exception),
        getattr(anthropic, "APIStatusError", Exception),
        getattr(anthropic, "APIConnectionError", Exception),
    ) as exc:
        return False, "", exc.__class__.__name__
    except Exception as exc:
        return False, "", exc.__class__.__name__


def run_llm_with_fallback(
    *,
    command: str,
    prompt: str,
    timeout_seconds: int,
    schema: dict[str, Any],
    fallback_model: str,
    invoker: Any = invoke_codex_cli,
    claude_invoker: Any = invoke_claude_api,
    anthropic_api_key: str | None = None,
) -> dict[str, Any]:
    ok, output, reason = invoker(command, prompt, timeout_seconds=timeout_seconds)
    if ok:
        return {"ok": True, "output": output, "reason": "", "engine": "codex"}
    if not _primary_unavailable_reason(reason):
        return {"ok": False, "output": output, "reason": reason or "codex-cli-failed", "engine": "codex"}
    key = os.environ.get("ANTHROPIC_API_KEY", "") if anthropic_api_key is None else anthropic_api_key
    if not str(key or "").strip():
        return {"ok": False, "output": output, "reason": reason or "codex-cli-failed", "engine": "codex"}
    ok2, output2, reason2 = claude_invoker(
        prompt,
        schema=schema,
        model=fallback_model,
    )
    if ok2:
        return {"ok": True, "output": output2, "reason": "", "engine": "claude"}
    return {"ok": False, "output": "", "reason": reason2 or "claude-api-failed", "engine": "claude"}


def parse_strict_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty output")
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(text)
    if text[end:].strip():
        raise ValueError("trailing non-json output")
    return value


def _valid_citations(raw: Any, allowed_urls: set[str]) -> list[str]:
    return [
        str(url).strip()
        for url in (raw or [])
        if str(url).strip() in allowed_urls
    ]


def _validate_scenarios(
    *,
    row: dict[str, Any],
    prob: float,
    allowed_urls: set[str],
    allowed_evidence_ids: set[str] | None = None,
    require_evidence: bool = False,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    raw_scenarios = row.get("scenarios")
    if not isinstance(raw_scenarios, list) or not 2 <= len(raw_scenarios) <= 4:
        return None, "scenarios must contain 2-4 branches"
    scenarios = []
    p_event_sum = 0.0
    point_sum = 0.0
    branches_with_evidence, evidence_errors = validate_branch_evidence(
        raw_scenarios,
        allowed_evidence_ids or set(),
    )
    if require_evidence and evidence_errors:
        return None, "; ".join(evidence_errors[:2])
    for idx, branch in enumerate(branches_with_evidence):
        if not isinstance(branch, dict):
            return None, f"scenario {idx} is not an object"
        event = str(branch.get("event") or "").strip()
        p_event = _prob_or_none(branch.get("p_event"))
        conditional = _prob_or_none(branch.get("p_outcome_given_event"))
        citations = _valid_citations(branch.get("citations"), allowed_urls)
        status = str(branch.get("status") or "supported")
        evidence_ids = [str(eid) for eid in (branch.get("evidence_ids") or [])]
        if not event:
            return None, f"scenario {idx} missing event"
        if p_event is None:
            return None, f"scenario {idx} invalid p_event"
        if conditional is None:
            return None, f"scenario {idx} invalid p_outcome_given_event"
        if not citations:
            return None, f"scenario {idx} missing valid citations"
        if require_evidence and status == "supported" and not evidence_ids:
            return None, f"scenario {idx} supported without evidence_ids"
        p_event_sum += p_event
        if status == "supported":
            point_sum += p_event * conditional
        scenarios.append({
            "event": event[:220],
            "p_event": p_event,
            "p_outcome_given_event": conditional,
            "citations": sorted(dict.fromkeys(citations)),
            "evidence_ids": sorted(dict.fromkeys(evidence_ids)),
            "status": status,
        })
    if abs(p_event_sum - 1.0) > SCENARIO_EVENT_SUM_TOLERANCE:
        return None, f"scenario p_event sum {p_event_sum:.4f} != 1.0"
    if require_evidence:
        tradeable_mass = branch_tradeable_mass(scenarios)
        if tradeable_mass < MIN_TRADEABLE_BRANCH_MASS:
            return None, f"supported branch mass {tradeable_mass:.4f} below {MIN_TRADEABLE_BRANCH_MASS:.2f}"
        if abs(point_sum - prob) > SCENARIO_PROB_SUM_TOLERANCE:
            return None, f"supported scenario weighted probability {point_sum:.4f} != qual_prob {prob:.4f}"
    elif abs(sum(b["p_event"] * b["p_outcome_given_event"] for b in scenarios) - prob) > SCENARIO_PROB_SUM_TOLERANCE:
        total = sum(b["p_event"] * b["p_outcome_given_event"] for b in scenarios)
        return None, f"scenario weighted probability {total:.4f} != qual_prob {prob:.4f}"
    return scenarios, None


def validate_qual_output(
    raw_output: str,
    *,
    allowed_tickers: set[str],
    allowed_urls: set[str],
    model_run_id: str,
    allowed_news_item_ids: set[str] | None = None,
    url_to_news_item_ids: dict[str, list[str]] | None = None,
    market_by_ticker: dict[str, dict[str, Any]] | None = None,
    retrieval_contexts: dict[str, list[dict[str, Any]]] | None = None,
    min_groundedness: float = 0.6,
    created_at: str | None = None,
    engine: str = "codex",
) -> QualValidationResult:
    parsed = parse_strict_json(raw_output)
    if not isinstance(parsed, list):
        raise ValueError("top-level JSON must be an array")
    created_at = created_at or utc_now()
    allowed_news_item_ids = allowed_news_item_ids or set()
    url_to_news_item_ids = url_to_news_item_ids or {}
    market_by_ticker = market_by_ticker or {}
    retrieval_contexts = retrieval_contexts or {}
    allowed_evidence_ids_by_ticker = {
        ticker: {
            str(ctx.get("evidence_id") or ctx.get("chunk_id"))
            for ctx in rows
            if ctx.get("context_role") == "evidence" or is_evidence_context(ctx)
            if ctx.get("evidence_id") or ctx.get("chunk_id")
        }
        for ticker, rows in retrieval_contexts.items()
    }
    accepted = []
    discarded = []
    retry_error = None
    for idx, row in enumerate(parsed):
        if not isinstance(row, dict):
            discarded.append({"index": idx, "reason": "entry is not an object"})
            continue
        ticker = str(row.get("ticker") or "").strip()
        if ticker not in allowed_tickers:
            discarded.append({"ticker": ticker, "reason": "ticker not in provided set"})
            continue
        prob = _safe_float(row.get("qual_prob"))
        confidence = _safe_float(row.get("confidence"))
        if prob is None:
            discarded.append({"ticker": ticker, "reason": "missing qual_prob"})
            continue
        prob = _clamp_prob(prob)
        base_rate = _prob_or_none(row.get("base_rate"))
        if base_rate is None:
            discarded.append({"ticker": ticker, "reason": "missing base_rate"})
            continue
        if confidence is None or confidence < MIN_CONFIDENCE:
            discarded.append({"ticker": ticker, "reason": f"confidence below {MIN_CONFIDENCE:.2f}"})
            continue
        citations = _valid_citations(row.get("citation_urls"), allowed_urls)
        if not citations:
            discarded.append({"ticker": ticker, "reason": "no valid citation_urls"})
            continue
        scenarios, scenario_error = _validate_scenarios(
            row=row,
            prob=prob,
            allowed_urls=allowed_urls,
            allowed_evidence_ids=allowed_evidence_ids_by_ticker.get(ticker),
            require_evidence=bool(retrieval_contexts.get(ticker)),
        )
        if scenario_error:
            retry_error = scenario_error
            discarded.append({"ticker": ticker, "reason": scenario_error})
            continue
        news_item_ids = [
            str(item_id).strip()
            for item_id in (row.get("news_item_ids_used") or [])
            if str(item_id).strip() in allowed_news_item_ids
        ]
        if not news_item_ids:
            for url in citations:
                news_item_ids.extend(url_to_news_item_ids.get(url, []))
        news_item_ids = sorted(dict.fromkeys(news_item_ids))
        analysis = {
            "ticker": ticker,
            "base_rate": round(base_rate, 6),
            "qual_prob": prob,
            "confidence": round(min(max(confidence, 0.0), 1.0), 6),
            "rationale": str(row.get("rationale") or "")[:600],
            "citation_urls": sorted(dict.fromkeys(citations)),
            "news_item_ids_used": news_item_ids,
            "scenarios": scenarios or [],
            "model_run_id": model_run_id,
            "prompt_version": QUAL_PROMPT_VERSION,
            "market": market_by_ticker.get(ticker, {}),
            "retrieved_context": retrieval_contexts.get(ticker, []),
            "created_at": created_at,
            "engine": engine,
        }
        score = groundedness_score(analysis, retrieval_contexts.get(ticker, []))
        tradeable = True
        blocked_reason = None
        if retrieval_contexts.get(ticker):
            if score.get("evidence_context_count", 0) <= 0:
                tradeable = False
                blocked_reason = "self-generated-evidence-only"
            if score["groundedness"] < float(min_groundedness):
                tradeable = False
                blocked_reason = "low-groundedness"
            if branch_tradeable_mass(scenarios or []) < MIN_TRADEABLE_BRANCH_MASS:
                tradeable = False
                blocked_reason = "unsupported-branch-mass"
        analysis["groundedness"] = score
        analysis["tradeable"] = tradeable
        analysis["blocked_reason"] = blocked_reason
        accepted.append({
            "ticker": ticker,
            "base_rate": analysis["base_rate"],
            "qual_prob": prob,
            "confidence": analysis["confidence"],
            "rationale": analysis["rationale"],
            "citation_urls": analysis["citation_urls"],
            "news_item_ids_used": news_item_ids,
            "scenarios": scenarios or [],
            "analysis": analysis,
            "groundedness": score["groundedness"],
            "groundedness_score": score,
            "tradeable": tradeable,
            "blocked_reason": blocked_reason,
            "created_at": created_at,
            "engine": engine,
            "model_run_id": model_run_id,
            "prompt_version": QUAL_PROMPT_VERSION,
            "signal_source": "qual",
        })
    return QualValidationResult(accepted=accepted, discarded=discarded, retry_error=retry_error)


def build_near_miss_prompt(
    *,
    news_items: list[dict[str, Any]],
    candidate: dict[str, Any],
    calibration_lines: list[str] | None = None,
    retry_error: str | None = None,
) -> str:
    schema = (
        '{"justified":false,"direction_consistent":false,"confidence":0.0,'
        '"rationale":"One or two sentences.",'
        '"citation_urls":["https://..."],"news_item_ids_used":["..."]}'
    )
    payload = {
        "market": {
            "ticker": candidate.get("ticker"),
            "title": candidate.get("label") or candidate.get("title"),
            "market_identity": candidate.get("market_identity"),
            "consensus_prob": candidate.get("model_prob"),
            "kalshi_price_prob": (
                (candidate.get("executable_price") or {}).get("price_prob")
                if isinstance(candidate.get("executable_price"), dict) else
                None
            ),
            "kalshi_price_cents": (
                (candidate.get("executable_price") or {}).get("price_cents")
                if isinstance(candidate.get("executable_price"), dict) else
                candidate.get("entry_price_cents")
            ),
            "raw_edge": candidate.get("raw_edge"),
            "net_edge": candidate.get("net_edge"),
            "required_edge": candidate.get("required_edge"),
            "gap": candidate.get("near_miss_gap"),
        },
        "news_items": news_items,
        "allowed_citation_urls": sorted({row["url"] for row in news_items if row.get("url")}),
        "allowed_news_item_ids": sorted({row["id"] for row in news_items if row.get("id")}),
        "calibration": calibration_lines or [],
        "prompt_version": "near-miss-qaq-v1",
    }
    retry_line = f"\nPrevious output was invalid: {retry_error}\n" if retry_error else ""
    return (
        "You are investigating a near-miss quantitative Kalshi sports edge. "
        "Use only the provided recent news items. Decide whether there is specific, "
        "citable news that justifies the observed price gap, and whether that news "
        "supports the same direction as the quantitative edge. "
        "Return STRICT JSON only, no markdown and no prose wrapper. "
        "If evidence is not specific, set justified=false. "
        "Every citation_url must be copied from allowed_citation_urls.\n"
        f"Schema: {schema}\n"
        f"{retry_line}"
        f"Input JSON:\n{json.dumps(payload, sort_keys=True)}"
    )


def validate_near_miss_output(
    raw_output: str,
    *,
    allowed_urls: set[str],
    allowed_news_item_ids: set[str],
    url_to_news_item_ids: dict[str, list[str]],
    news_by_id: dict[str, dict[str, Any]],
    candidate: dict[str, Any],
    model_run_id: str,
    engine: str = "codex",
) -> dict[str, Any]:
    parsed = parse_strict_json(raw_output)
    if not isinstance(parsed, dict):
        raise ValueError("top-level JSON must be an object")
    confidence = _safe_float(parsed.get("confidence"))
    if confidence is None:
        raise ValueError("missing confidence")
    citations = _valid_citations(parsed.get("citation_urls"), allowed_urls)
    justified = bool(parsed.get("justified"))
    direction_consistent = bool(parsed.get("direction_consistent"))
    if justified and not citations:
        raise ValueError("justified near-miss requires valid citations")
    news_item_ids = [
        str(item_id).strip()
        for item_id in (parsed.get("news_item_ids_used") or [])
        if str(item_id).strip() in allowed_news_item_ids
    ]
    if not news_item_ids:
        for url in citations:
            news_item_ids.extend(url_to_news_item_ids.get(url, []))
    news_item_ids = sorted(dict.fromkeys(news_item_ids))
    first_seen_at = None
    source = None
    for item_id in news_item_ids:
        item = news_by_id.get(item_id) or {}
        first_seen_at = item.get("published_at") or item.get("fetched_at") or first_seen_at
        source = item.get("source") or source
        if first_seen_at and source:
            break
    return {
        "ticker": candidate.get("ticker"),
        "candidate_id": candidate.get("candidate_id"),
        "status": "ok",
        "justified": justified,
        "direction_consistent": direction_consistent,
        "confidence": round(min(max(confidence, 0.0), 1.0), 6),
        "rationale": str(parsed.get("rationale") or "")[:700],
        "citation_urls": sorted(dict.fromkeys(citations)),
        "news_item_ids": news_item_ids,
        "first_seen_at": first_seen_at,
        "source": source,
        "gap": candidate.get("near_miss_gap"),
        "net_edge": candidate.get("net_edge"),
        "required_edge": candidate.get("required_edge"),
        "consensus_prob": candidate.get("model_prob"),
        "kalshi_price_cents": (
            (candidate.get("executable_price") or {}).get("price_cents")
            if isinstance(candidate.get("executable_price"), dict) else
            candidate.get("entry_price_cents")
        ),
        "model_run_id": model_run_id,
        "engine": engine,
        "signal_source": "qual_activated_quant",
        "created_at": utc_now(),
    }


def run_near_miss_investigations(
    *,
    command: str,
    news_items: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    timeout_seconds: int,
    calibration_lines: list[str] | None = None,
    invoker: Any = invoke_codex_cli,
    claude_invoker: Any = invoke_claude_api,
    fallback_model: str = "claude-opus-4-8",
    anthropic_api_key: str | None = None,
) -> dict[str, Any]:
    model_run_id = "near-miss-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if not candidates:
        return {
            "status": "ok",
            "reason": "no-near-miss-candidates",
            "model_run_id": model_run_id,
            "investigations": [],
        }
    allowed_urls = {str(row.get("url")) for row in news_items if row.get("url")}
    allowed_news_item_ids = {str(row.get("id")) for row in news_items if row.get("id")}
    url_to_news_item_ids: dict[str, list[str]] = {}
    news_by_id: dict[str, dict[str, Any]] = {}
    for item in news_items:
        if item.get("id"):
            news_by_id[str(item["id"])] = item
        if item.get("url") and item.get("id"):
            url_to_news_item_ids.setdefault(str(item["url"]), []).append(str(item["id"]))
    if not news_items:
        return {
            "status": "ok",
            "reason": "no-recent-news-items",
            "model_run_id": model_run_id,
            "investigations": [
                {
                    "ticker": row.get("ticker"),
                    "candidate_id": row.get("candidate_id"),
                    "status": "blocked",
                    "reason": "no-recent-news-items",
                    "justified": False,
                    "direction_consistent": False,
                    "confidence": 0.0,
                    "rationale": "No recent citable news items were available for investigation.",
                    "citation_urls": [],
                    "news_item_ids": [],
                    "gap": row.get("near_miss_gap"),
                    "net_edge": row.get("net_edge"),
                    "required_edge": row.get("required_edge"),
                    "model_run_id": model_run_id,
                    "engine": "codex",
                    "signal_source": "qual_activated_quant",
                    "created_at": utc_now(),
                }
                for row in candidates
            ],
        }

    investigations: list[dict[str, Any]] = []
    unavailable = 0
    for candidate in candidates:
        retry_error = None
        last_reason = None
        for attempt in (1, 2):
            prompt = build_near_miss_prompt(
                news_items=news_items,
                candidate=candidate,
                calibration_lines=calibration_lines,
                retry_error=retry_error,
            )
            run = run_llm_with_fallback(
                command=command,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                schema=NEAR_MISS_INVESTIGATION_SCHEMA,
                fallback_model=fallback_model,
                invoker=invoker,
                claude_invoker=claude_invoker,
                anthropic_api_key=anthropic_api_key,
            )
            engine = str(run.get("engine") or "codex")
            if not run.get("ok"):
                unavailable += 1
                investigations.append({
                    "ticker": candidate.get("ticker"),
                    "candidate_id": candidate.get("candidate_id"),
                    "status": "unavailable",
                    "reason": run.get("reason") or "codex-cli-failed",
                    "justified": False,
                    "direction_consistent": False,
                    "confidence": 0.0,
                    "rationale": "Near-miss investigation engine unavailable.",
                    "citation_urls": [],
                    "news_item_ids": [],
                    "gap": candidate.get("near_miss_gap"),
                    "net_edge": candidate.get("net_edge"),
                    "required_edge": candidate.get("required_edge"),
                    "model_run_id": model_run_id,
                    "engine": engine,
                    "signal_source": "qual_activated_quant",
                    "created_at": utc_now(),
                })
                break
            try:
                investigations.append(validate_near_miss_output(
                    str(run.get("output") or ""),
                    allowed_urls=allowed_urls,
                    allowed_news_item_ids=allowed_news_item_ids,
                    url_to_news_item_ids=url_to_news_item_ids,
                    news_by_id=news_by_id,
                    candidate=candidate,
                    model_run_id=model_run_id,
                    engine=engine,
                ))
                break
            except ValueError as exc:
                last_reason = str(exc)
                retry_error = last_reason
                if attempt == 2:
                    investigations.append({
                        "ticker": candidate.get("ticker"),
                        "candidate_id": candidate.get("candidate_id"),
                        "status": "invalid",
                        "reason": f"malformed-json-after-retry: {last_reason}",
                        "justified": False,
                        "direction_consistent": False,
                        "confidence": 0.0,
                        "rationale": "Near-miss investigation output failed validation.",
                        "citation_urls": [],
                        "news_item_ids": [],
                        "gap": candidate.get("near_miss_gap"),
                        "net_edge": candidate.get("net_edge"),
                        "required_edge": candidate.get("required_edge"),
                        "model_run_id": model_run_id,
                        "engine": engine,
                        "signal_source": "qual_activated_quant",
                        "created_at": utc_now(),
                    })
    return {
        "status": "unavailable" if unavailable == len(candidates) else "ok",
        "reason": "engine-unavailable" if unavailable == len(candidates) else None,
        "model_run_id": model_run_id,
        "investigation_count": len(investigations),
        "investigations": investigations,
    }


def run_qual_model(
    *,
    command: str,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    timeout_seconds: int,
    retrieval_contexts: dict[str, list[dict[str, Any]]] | None = None,
    min_groundedness: float = 0.6,
    lessons: list[dict[str, Any]] | None = None,
    calibration_lines: list[str] | None = None,
    invoker: Any = invoke_codex_cli,
    claude_invoker: Any = invoke_claude_api,
    fallback_model: str = "claude-opus-4-8",
    anthropic_api_key: str | None = None,
) -> dict[str, Any]:
    model_run_id = "qual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    allowed_tickers = {str(row.get("ticker")) for row in markets if row.get("ticker")}
    allowed_urls = {str(row.get("url")) for row in news_items if row.get("url")}
    allowed_news_item_ids = {str(row.get("id")) for row in news_items if row.get("id")}
    url_to_news_item_ids: dict[str, list[str]] = {}
    for item in news_items:
        if item.get("url") and item.get("id"):
            url_to_news_item_ids.setdefault(str(item["url"]), []).append(str(item["id"]))
    market_by_ticker = {str(row["ticker"]): row for row in markets if row.get("ticker")}
    retrieval_contexts = retrieval_contexts or {}
    if not markets:
        return {
            "status": "ok",
            "reason": "no-unpriced-team-markets",
            "model_run_id": model_run_id,
            "signals": [],
            "discarded": [],
        }
    if not news_items:
        return {
            "status": "ok",
            "reason": "no-recent-news-items",
            "model_run_id": model_run_id,
            "signals": [],
            "discarded": [],
        }

    retry_error = None
    last_reason = None
    last_output = ""
    for attempt in (1, 2):
        prompt = build_prompt(
            news_items=news_items,
            markets=markets,
            retrieval_contexts=retrieval_contexts,
            lessons=lessons,
            calibration_lines=calibration_lines,
            retry_error=retry_error,
        )
        run = run_llm_with_fallback(
            command=command,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            schema=QUAL_SIGNAL_ARRAY_SCHEMA,
            fallback_model=fallback_model,
            invoker=invoker,
            claude_invoker=claude_invoker,
            anthropic_api_key=anthropic_api_key,
        )
        output = str(run.get("output") or "")
        engine = str(run.get("engine") or "codex")
        last_output = output
        if not run.get("ok"):
            return {
                "status": "unavailable",
                "reason": run.get("reason") or "codex-cli-failed",
                "model_run_id": model_run_id,
                "attempts": attempt,
                "engine": engine,
                "signals": [],
                "discarded": [],
            }
        try:
            validation = validate_qual_output(
                output,
                allowed_tickers=allowed_tickers,
                allowed_urls=allowed_urls,
                model_run_id=model_run_id,
                allowed_news_item_ids=allowed_news_item_ids,
                url_to_news_item_ids=url_to_news_item_ids,
                market_by_ticker=market_by_ticker,
                retrieval_contexts=retrieval_contexts,
                min_groundedness=min_groundedness,
                engine=engine,
            )
        except ValueError as exc:
            last_reason = str(exc)
            retry_error = last_reason
            continue
        if validation.retry_error and attempt == 1 and not validation.accepted:
            last_reason = validation.retry_error
            retry_error = validation.retry_error
            continue
        return {
            "status": "ok",
            "reason": None,
            "model_run_id": model_run_id,
            "attempts": attempt,
            "engine": engine,
            "signals": validation.accepted,
            "discarded": validation.discarded,
            "retrieval_contexts": retrieval_contexts,
        }

    if USAGE_LIMIT_RE.search(last_output):
        last_reason = "usage-limit"
    return {
        "status": "unavailable",
        "reason": f"malformed-json-after-retry: {last_reason or 'invalid output'}",
        "model_run_id": model_run_id,
        "attempts": 2,
        "engine": "codex",
        "signals": [],
        "discarded": [],
    }
