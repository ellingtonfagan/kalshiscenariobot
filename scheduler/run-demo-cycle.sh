#!/usr/bin/env bash
set -u

REPO="${NBABOT_REPO:-/Users/ellingtonfagan/Downloads/nba-scenario-bot}"
KSOBOT="${KSOBOT:-$REPO/.venv/bin/ksobot}"

cd "$REPO" || exit 2
mkdir -p "${NBABOT_LOG_DIR:-$REPO/logs}"

export NBABOT_EXECUTION_MODE=demo
export NBABOT_DELIVER_TO="${NBABOT_DELIVER_TO:-telegram}"

if [ ! -x "$KSOBOT" ]; then
  echo "[run-demo-cycle] missing executable: $KSOBOT" >&2
  exit 127
fi

exec "$KSOBOT" scheduled-demo-cycle "$@"
