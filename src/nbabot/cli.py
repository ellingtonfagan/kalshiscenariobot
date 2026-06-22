"""CLI entrypoint:  nbabot <phase>  /  python -m nbabot <phase>."""
from __future__ import annotations

import argparse
import sys

from .agents import PHASES
from .agents.base import load_context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nbabot", description=__doc__)
    parser.add_argument("phase", choices=sorted(PHASES.keys()),
                        help="game phase to run")
    parser.add_argument("--game-id", default=None,
                        help="override NBABOT_GAME_ID (config/<id>.*.yaml)")
    args = parser.parse_args(argv)

    try:
        ctx = load_context(args.game_id)
    except FileNotFoundError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    PHASES[args.phase](ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
