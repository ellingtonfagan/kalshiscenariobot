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

from .research import utc_now
from .research_news import ResearchTeam, item_matches_team


MIN_CONFIDENCE = 0.55
USAGE_LIMIT_RE = re.compile(r"(usage limit|quota|rate limit|limit reached|too many requests)", re.I)


@dataclass(frozen=True)
class QualValidationResult:
    accepted: list[dict[str, Any]]
    discarded: list[dict[str, Any]]


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
            "team": team.key,
            "source": item.get("source"),
            "title": item.get("title"),
            "summary": str(item.get("body") or "")[:900],
            "url": item.get("url"),
            "published_at": item.get("published_at"),
        })
    rows.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
    return rows[:max_items]


def build_prompt(
    *,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    retry_error: str | None = None,
) -> str:
    schema = (
        '[{"ticker":"...","qual_prob":0.50,"confidence":0.70,'
        '"rationale":"One or two sentences.","citation_urls":["https://..."]}]'
    )
    payload = {
        "news_items": news_items,
        "markets": markets,
        "allowed_tickers": [row["ticker"] for row in markets],
        "allowed_citation_urls": sorted({row["url"] for row in news_items if row.get("url")}),
    }
    retry_line = f"\nPrevious output was invalid: {retry_error}\n" if retry_error else ""
    return (
        "You are pricing Kalshi sports markets that lack sportsbook consensus. "
        "Use only the provided news/discussion items and market rows. "
        "Return STRICT JSON only, with no markdown, no prose wrapper, and no comments. "
        "Every citation_url must be copied from allowed_citation_urls. "
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


def parse_strict_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty output")
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(text)
    if text[end:].strip():
        raise ValueError("trailing non-json output")
    return value


def validate_qual_output(
    raw_output: str,
    *,
    allowed_tickers: set[str],
    allowed_urls: set[str],
    model_run_id: str,
    created_at: str | None = None,
) -> QualValidationResult:
    parsed = parse_strict_json(raw_output)
    if not isinstance(parsed, list):
        raise ValueError("top-level JSON must be an array")
    created_at = created_at or utc_now()
    accepted = []
    discarded = []
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
        if confidence is None or confidence < MIN_CONFIDENCE:
            discarded.append({"ticker": ticker, "reason": f"confidence below {MIN_CONFIDENCE:.2f}"})
            continue
        citations = [
            str(url).strip()
            for url in (row.get("citation_urls") or [])
            if str(url).strip() in allowed_urls
        ]
        if not citations:
            discarded.append({"ticker": ticker, "reason": "no valid citation_urls"})
            continue
        accepted.append({
            "ticker": ticker,
            "qual_prob": _clamp_prob(prob),
            "confidence": round(min(max(confidence, 0.0), 1.0), 6),
            "rationale": str(row.get("rationale") or "")[:600],
            "citation_urls": sorted(dict.fromkeys(citations)),
            "created_at": created_at,
            "model_run_id": model_run_id,
            "signal_source": "qual",
        })
    return QualValidationResult(accepted=accepted, discarded=discarded)


def run_qual_model(
    *,
    command: str,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    timeout_seconds: int,
    invoker: Any = invoke_codex_cli,
) -> dict[str, Any]:
    model_run_id = "qual-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    allowed_tickers = {str(row.get("ticker")) for row in markets if row.get("ticker")}
    allowed_urls = {str(row.get("url")) for row in news_items if row.get("url")}
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
        prompt = build_prompt(news_items=news_items, markets=markets, retry_error=retry_error)
        ok, output, reason = invoker(command, prompt, timeout_seconds=timeout_seconds)
        last_output = output
        if not ok:
            return {
                "status": "unavailable",
                "reason": reason or "codex-cli-failed",
                "model_run_id": model_run_id,
                "attempts": attempt,
                "signals": [],
                "discarded": [],
            }
        try:
            validation = validate_qual_output(
                output,
                allowed_tickers=allowed_tickers,
                allowed_urls=allowed_urls,
                model_run_id=model_run_id,
            )
        except ValueError as exc:
            last_reason = str(exc)
            retry_error = last_reason
            continue
        return {
            "status": "ok",
            "reason": None,
            "model_run_id": model_run_id,
            "attempts": attempt,
            "signals": validation.accepted,
            "discarded": validation.discarded,
        }

    if USAGE_LIMIT_RE.search(last_output):
        last_reason = "usage-limit"
    return {
        "status": "unavailable",
        "reason": f"malformed-json-after-retry: {last_reason or 'invalid output'}",
        "model_run_id": model_run_id,
        "attempts": 2,
        "signals": [],
        "discarded": [],
    }
