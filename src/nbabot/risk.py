"""Pre-execution risk gate. All checks must pass before paper/demo orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guardrails import MAX_STAKE_UNITS

RESEARCH_OVERRIDE_ACK = "RESEARCH_OVERRIDE_APPROVED"
RESEARCH_OVERRIDE_MAX_UNITS = 1.0
MIN_RESEARCH_OVERRIDE_REASON_CHARS = 80
MIN_RESEARCH_OVERRIDE_SOURCES = 2
BROAD_SLATE_TRADES_PER_VALIDATED_FAMILY = 2
DEFAULT_PAPER_DEMO_DAILY_TRADE_CAP = 50
DEFAULT_QUAL_DAILY_TRADE_CAP = 10


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class RiskContext:
    game_exposure_units: float = 0.0
    portfolio_exposure_units: float = 0.0
    daily_pnl_units: float = 0.0
    open_positions: int = 0
    last_trade_lost: bool = False
    last_loss_stake_units: float = 0.0
    broad_slate_trade_count: int = 0
    broad_slate_daily_trade_limit: int | None = None
    paper_demo_broad_slate_trade_count: int = 0
    paper_demo_daily_trade_cap: int | None = None
    qual_daily_trade_count: int = 0
    qual_daily_trade_cap: int | None = None


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    checks: list[RiskCheck] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [c.reason for c in self.checks if not c.passed]


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _research_override_status(intent: Any, settings: Any,
                              stake_units: float) -> tuple[bool, bool, str]:
    requested = bool(getattr(intent, "research_override", False))
    if not requested:
        return False, False, "research override not requested"

    ack_ok = (
        getattr(settings, "research_override_ack", "")
        == RESEARCH_OVERRIDE_ACK
    )
    approved_by = str(getattr(intent, "research_approved_by", "") or "").strip()
    reason = str(getattr(intent, "research_override_reason", "") or "").strip()
    sources = tuple(
        str(source).strip()
        for source in (getattr(intent, "research_sources", ()) or ())
        if str(source).strip()
    )
    configured_cap = min(
        float(getattr(
            settings,
            "research_override_max_units",
            RESEARCH_OVERRIDE_MAX_UNITS,
        )),
        RESEARCH_OVERRIDE_MAX_UNITS,
    )
    stake_ok = 0 < stake_units <= configured_cap
    reason_ok = len(reason) >= MIN_RESEARCH_OVERRIDE_REASON_CHARS
    sources_ok = len(set(sources)) >= MIN_RESEARCH_OVERRIDE_SOURCES
    approved = all((ack_ok, bool(approved_by), stake_ok, reason_ok, sources_ok))

    failures = []
    if not ack_ok:
        failures.append(
            f"set NBABOT_RESEARCH_OVERRIDE_ACK={RESEARCH_OVERRIDE_ACK}"
        )
    if not approved_by:
        failures.append("missing named human approver")
    if not stake_ok:
        failures.append(f"override stake must be >0 and <={configured_cap:.3f}u")
    if not reason_ok:
        failures.append(
            "research rationale must be at least "
            f"{MIN_RESEARCH_OVERRIDE_REASON_CHARS} characters"
        )
    if not sources_ok:
        failures.append(
            f"at least {MIN_RESEARCH_OVERRIDE_SOURCES} distinct research sources required"
        )

    if failures:
        return True, False, "; ".join(failures)
    return (
        True,
        True,
        f"approved by {approved_by}; {len(set(sources))} sources; "
        f"stake {stake_units:.3f}u <= {configured_cap:.3f}u",
    )


def validated_market_family_count(learning: dict[str, Any] | None) -> int:
    families = (learning or {}).get("families") or {}
    return sum(
        1
        for row in families.values()
        if isinstance(row, dict) and bool(row.get("validated"))
    )


def broad_slate_daily_trade_limit(
    learning: dict[str, Any] | None,
    *,
    per_family: int = BROAD_SLATE_TRADES_PER_VALIDATED_FAMILY,
) -> int:
    """Small linear ramp: two trades per validated family keeps a $100 roll tight."""
    return max(validated_market_family_count(learning) * max(int(per_family), 0), 0)


def evaluate_trade_intent(intent: Any, settings: Any,
                          context: RiskContext | None = None) -> RiskDecision:
    context = context or RiskContext()
    checks: list[RiskCheck] = []
    execution_mode = str(getattr(settings, "execution_mode", "paper") or "paper").lower()
    live_mode = execution_mode == "live"
    paper_demo_mode = execution_mode in {"paper", "demo"}
    signal_source = str(getattr(intent, "signal_source", "consensus") or "consensus")

    kill_switch = Path(settings.kill_switch_path)
    checks.append(RiskCheck(
        "kill_switch",
        not kill_switch.exists(),
        f"kill switch present at {kill_switch}" if kill_switch.exists() else "kill switch clear",
    ))

    stake_units = float(getattr(intent, "stake_units", 0.0))
    override_requested, override_approved, override_reason = (
        _research_override_status(intent, settings, stake_units)
    )
    checks.append(RiskCheck(
        "stake_cap",
        0 < stake_units <= MAX_STAKE_UNITS,
        (
            f"stake {stake_units:.3f} units must be >0 and "
            f"<={MAX_STAKE_UNITS:g}"
        ),
    ))

    new_exposure = context.game_exposure_units + stake_units
    max_game = float(settings.max_game_exposure_units)
    checks.append(RiskCheck(
        "game_exposure",
        new_exposure <= max_game,
        f"game exposure {new_exposure:.3f} units <= max {max_game:.3f}",
    ))

    portfolio_exposure = context.portfolio_exposure_units + stake_units
    max_daily_exposure = float(getattr(
        settings,
        "max_daily_exposure_units",
        MAX_STAKE_UNITS,
    ))
    checks.append(RiskCheck(
        "daily_portfolio_exposure",
        portfolio_exposure <= max_daily_exposure,
        (
            f"daily portfolio exposure {portfolio_exposure:.3f} units <= "
            f"max {max_daily_exposure:.3f}"
        ),
    ))

    max_loss = float(settings.max_daily_loss_units)
    checks.append(RiskCheck(
        "daily_loss",
        context.daily_pnl_units >= -max_loss,
        f"daily P&L {context.daily_pnl_units:.3f} units vs loss limit -{max_loss:.3f}",
    ))

    loss_chase_ok = not (
        context.last_trade_lost and stake_units > context.last_loss_stake_units
    )
    checks.append(RiskCheck(
        "no_loss_chasing",
        loss_chase_ok,
        "stake does not increase after a loss" if loss_chase_ok
        else "stake increases after a loss",
    ))

    sgp_p = getattr(intent, "sgp_adjusted_prob", None)
    checks.append(RiskCheck(
        "sgp_probability",
        sgp_p is not None and 0 < float(sgp_p) < 1,
        "SGP-adjusted probability present" if sgp_p is not None
        else "missing SGP-adjusted probability",
    ))

    ticker = getattr(intent, "ticker", None)
    checks.append(RiskCheck(
        "tradable_mapping",
        bool(ticker),
        f"ticker {ticker} mapped" if ticker else "missing tradable Kalshi ticker",
    ))

    qual_live_ok = not (live_mode and signal_source == "qual")
    checks.append(RiskCheck(
        "qual_live_block",
        qual_live_ok,
        (
            "qual-sourced intents are hard-blocked in live mode"
            if not qual_live_ok else
            "qual live block not applicable"
            if signal_source == "qual" else
            "not a qual-sourced intent"
        ),
    ))

    broad_slate = bool(getattr(intent, "broad_slate", False))
    family_validated = bool(getattr(intent, "validated", False))
    family = str(getattr(intent, "market_family", "") or "unknown")
    family_validation_passed = (not broad_slate) or (not live_mode) or family_validated
    checks.append(RiskCheck(
        "broad_slate_family_validation",
        family_validation_passed,
        (
            f"broad-slate market family {family} is validated"
            if family_validated else
            f"broad-slate market family {family} is not validated"
            if broad_slate and live_mode else
            "broad-slate family validation applies only in live mode"
            if broad_slate else
            "not a broad-slate intent"
        ),
    ))

    if broad_slate and live_mode:
        daily_limit = max(int(context.broad_slate_daily_trade_limit or 0), 0)
        current_trades = max(int(context.broad_slate_trade_count or 0), 0)
        checks.append(RiskCheck(
            "broad_slate_daily_trade_limit",
            current_trades < daily_limit,
            (
                f"broad-slate daily trades {current_trades + 1} <= "
                f"validated-family limit {daily_limit}"
            ),
        ))
    elif broad_slate:
        checks.append(RiskCheck(
            "broad_slate_daily_trade_limit",
            True,
            "validated-family daily limit applies only in live mode",
        ))
    else:
        checks.append(RiskCheck(
            "broad_slate_daily_trade_limit",
            True,
            "not a broad-slate intent",
        ))

    if broad_slate and paper_demo_mode:
        configured_cap = (
            context.paper_demo_daily_trade_cap
            if context.paper_demo_daily_trade_cap is not None
            else getattr(
                settings,
                "paper_demo_daily_trade_cap",
                DEFAULT_PAPER_DEMO_DAILY_TRADE_CAP,
            )
        )
        daily_cap = max(int(configured_cap), 0)
        paper_demo_trades = max(int(context.paper_demo_broad_slate_trade_count or 0), 0)
        cap_ok = paper_demo_trades < daily_cap
        checks.append(RiskCheck(
            "paper_demo_broad_slate_daily_trade_cap",
            cap_ok,
            (
                f"paper/demo broad-slate daily trades {paper_demo_trades + 1} "
                f"<= cap {daily_cap}"
                if cap_ok else
                f"paper/demo broad-slate daily cap {daily_cap} reached; "
                f"order {paper_demo_trades + 1} blocked"
            ),
        ))
    else:
        checks.append(RiskCheck(
            "paper_demo_broad_slate_daily_trade_cap",
            True,
            (
                "paper/demo broad-slate cap not applicable"
                if broad_slate else
                "not a broad-slate intent"
            ),
        ))

    if signal_source == "qual" and paper_demo_mode:
        configured_qual_cap = (
            context.qual_daily_trade_cap
            if context.qual_daily_trade_cap is not None
            else getattr(settings, "qual_daily_trade_cap", DEFAULT_QUAL_DAILY_TRADE_CAP)
        )
        qual_cap = max(int(configured_qual_cap), 0)
        qual_trades = max(int(context.qual_daily_trade_count or 0), 0)
        qual_cap_ok = qual_trades < qual_cap
        checks.append(RiskCheck(
            "qual_daily_trade_cap",
            qual_cap_ok,
            (
                f"qual daily trades {qual_trades + 1} <= cap {qual_cap}"
                if qual_cap_ok else
                f"qual daily trade cap {qual_cap} reached; order {qual_trades + 1} blocked"
            ),
        ))
    else:
        checks.append(RiskCheck(
            "qual_daily_trade_cap",
            True,
            (
                "qual daily cap applies only in paper/demo mode"
                if signal_source == "qual" else
                "not a qual-sourced intent"
            ),
        ))

    edge = getattr(intent, "edge", None)
    min_edge = (
        float(getattr(settings, "qual_min_edge", 0.06))
        if signal_source == "qual" else
        float(getattr(
            settings,
            "execution_min_edge",
            getattr(settings, "min_edge", 0.05),
        ))
    )
    edge_ok = edge is not None and float(edge) >= min_edge
    if override_requested:
        checks.append(RiskCheck(
            "research_override",
            override_approved,
            override_reason,
        ))
    edge_passed = edge_ok or (
        edge is not None and override_requested and override_approved
    )
    checks.append(RiskCheck(
        "edge",
        edge_passed,
        (
            f"edge {float(edge):+.3f} meets min {min_edge:.3f}"
            if edge_ok else
            f"edge {float(edge):+.3f} below min {min_edge:.3f}; "
            "approved research override applied"
            if edge_passed else
            f"edge {float(edge):+.3f} below min {min_edge:.3f}"
            if edge is not None else
            "missing edge"
        ),
    ))

    captured_at = _parse_ts(getattr(intent, "captured_at", None))
    now = datetime.now(timezone.utc)
    age = (now - captured_at).total_seconds() if captured_at else None
    stale_ok = age is not None and age <= int(settings.stale_market_seconds)
    checks.append(RiskCheck(
        "stale_data",
        stale_ok,
        f"market data age {age:.0f}s <= {settings.stale_market_seconds}s"
        if age is not None else "missing market timestamp",
    ))

    bid = getattr(intent, "bid_cents", None)
    ask = getattr(intent, "ask_cents", None)
    spread = (int(ask) - int(bid)) if bid is not None and ask is not None else None
    spread_ok = spread is not None and 0 <= spread <= int(settings.max_spread_cents)
    checks.append(RiskCheck(
        "liquidity",
        spread_ok,
        f"spread {spread}c <= max {settings.max_spread_cents}c"
        if spread is not None else "missing bid/ask spread",
    ))

    risk = int(getattr(intent, "risk", 0) or 0)
    hope_bet = bool(getattr(intent, "hope_bet", False))
    checks.append(RiskCheck(
        "hope_bet_flag",
        risk < 5 or hope_bet,
        "risk-5 scenario explicitly flagged as hope bet"
        if risk >= 5 and hope_bet else "hope-bet flag not required"
        if risk < 5 else "risk-5 scenario missing hope-bet flag",
    ))

    return RiskDecision(approved=all(c.passed for c in checks), checks=checks)
