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
from .research import utc_now
from .research_news import ResearchTeam, item_matches_team


MIN_CONFIDENCE = 0.55
USAGE_LIMIT_RE = re.compile(r"(usage limit|quota|rate limit|limit reached|too many requests)", re.I)
QUAL_PROMPT_VERSION = "qual-scenario-v1"
SCENARIO_EVENT_SUM_TOLERANCE = 0.025
SCENARIO_PROB_SUM_TOLERANCE = 0.015


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


def build_prompt(
    *,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    lessons: list[dict[str, Any]] | None = None,
    calibration_lines: list[str] | None = None,
    retry_error: str | None = None,
) -> str:
    schema = (
        '[{"ticker":"...","base_rate":0.50,"qual_prob":0.50,"confidence":0.70,'
        '"rationale":"One or two sentences.",'
        '"scenarios":[{"event":"short branch description","p_event":0.50,'
        '"p_outcome_given_event":0.60,"citations":["https://..."]}],'
        '"citation_urls":["https://..."],"news_item_ids_used":["..."]}]'
    )
    payload = {
        "news_items": news_items,
        "markets": markets,
        "allowed_tickers": [row["ticker"] for row in markets],
        "allowed_citation_urls": sorted({row["url"] for row in news_items if row.get("url")}),
        "allowed_news_item_ids": sorted({row["id"] for row in news_items if row.get("id")}),
        "lessons": lessons or [],
        "calibration": calibration_lines or [],
        "prompt_version": QUAL_PROMPT_VERSION,
    }
    retry_line = f"\nPrevious output was invalid: {retry_error}\n" if retry_error else ""
    return (
        "You are pricing Kalshi sports markets that lack sportsbook consensus. "
        "Use only the provided news/discussion items and market rows. "
        "Decompose every forecast into 2-4 mutually exclusive, collectively exhaustive scenario branches. "
        "The scenario p_event values must sum to about 1.0, and qual_prob must equal "
        "sum(p_event * p_outcome_given_event) within rounding. "
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
) -> tuple[list[dict[str, Any]] | None, str | None]:
    raw_scenarios = row.get("scenarios")
    if not isinstance(raw_scenarios, list) or not 2 <= len(raw_scenarios) <= 4:
        return None, "scenarios must contain 2-4 branches"
    scenarios = []
    p_event_sum = 0.0
    point_sum = 0.0
    for idx, branch in enumerate(raw_scenarios):
        if not isinstance(branch, dict):
            return None, f"scenario {idx} is not an object"
        event = str(branch.get("event") or "").strip()
        p_event = _prob_or_none(branch.get("p_event"))
        conditional = _prob_or_none(branch.get("p_outcome_given_event"))
        citations = _valid_citations(branch.get("citations"), allowed_urls)
        if not event:
            return None, f"scenario {idx} missing event"
        if p_event is None:
            return None, f"scenario {idx} invalid p_event"
        if conditional is None:
            return None, f"scenario {idx} invalid p_outcome_given_event"
        if not citations:
            return None, f"scenario {idx} missing valid citations"
        p_event_sum += p_event
        point_sum += p_event * conditional
        scenarios.append({
            "event": event[:220],
            "p_event": p_event,
            "p_outcome_given_event": conditional,
            "citations": sorted(dict.fromkeys(citations)),
        })
    if abs(p_event_sum - 1.0) > SCENARIO_EVENT_SUM_TOLERANCE:
        return None, f"scenario p_event sum {p_event_sum:.4f} != 1.0"
    if abs(point_sum - prob) > SCENARIO_PROB_SUM_TOLERANCE:
        return None, f"scenario weighted probability {point_sum:.4f} != qual_prob {prob:.4f}"
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
    created_at: str | None = None,
) -> QualValidationResult:
    parsed = parse_strict_json(raw_output)
    if not isinstance(parsed, list):
        raise ValueError("top-level JSON must be an array")
    created_at = created_at or utc_now()
    allowed_news_item_ids = allowed_news_item_ids or set()
    url_to_news_item_ids = url_to_news_item_ids or {}
    market_by_ticker = market_by_ticker or {}
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
            "created_at": created_at,
        }
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
            "created_at": created_at,
            "model_run_id": model_run_id,
            "prompt_version": QUAL_PROMPT_VERSION,
            "signal_source": "qual",
        })
    return QualValidationResult(accepted=accepted, discarded=discarded, retry_error=retry_error)


def run_qual_model(
    *,
    command: str,
    news_items: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    timeout_seconds: int,
    lessons: list[dict[str, Any]] | None = None,
    calibration_lines: list[str] | None = None,
    invoker: Any = invoke_codex_cli,
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
            lessons=lessons,
            calibration_lines=calibration_lines,
            retry_error=retry_error,
        )
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
                allowed_news_item_ids=allowed_news_item_ids,
                url_to_news_item_ids=url_to_news_item_ids,
                market_by_ticker=market_by_ticker,
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
