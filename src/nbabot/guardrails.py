"""§7 standing orders — ALWAYS ON. Do not weaken (see AGENTS.md §0).

tests/test_smoke.py asserts these stay intact. If you remove the assertions to make a
build pass, you have broken the contract this whole project exists to honor.
"""
from __future__ import annotations

MAX_STAKE_UNITS = 5.0

GUARDRAIL_FOOTER = (
    "Bet only what you can lose. NY help: 877-8-HOPENY / text HOPENY (467369)."
)

STANDING_ORDERS = [
    "Show SGP-adjusted payout, never a naive multiply.",
    (
        f"Suggested stake <= {MAX_STAKE_UNITS:g} units; stake increases after a loss "
        "require a viable edge and explicit human approval."
    ),
    "Refuse to 'find' a target payout by stacking longshots; report true joint probability.",
    "Flag risk-5 scenarios as hope bets explicitly.",
    f"Append: '{GUARDRAIL_FOOTER}'",
]


def is_hope_bet(risk: int) -> bool:
    return risk >= 5


def with_footer(text: str) -> str:
    """Ensure any bet-related output carries the helpline footer exactly once."""
    if GUARDRAIL_FOOTER in text:
        return text
    return text.rstrip() + "\n\n" + GUARDRAIL_FOOTER
