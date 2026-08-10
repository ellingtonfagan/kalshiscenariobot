#!/usr/bin/env bash
set -u

# Raise the file-descriptor soft limit before python starts. macOS cron/launchd
# inherit a 256 soft limit which was exhausted mid-cycle 2026-07-31 with
# `OSError: [Errno 24] Too many open files` after the 200-candidate matcher
# phase, so cycles ran ~90% of the way and then died before writing their
# summary. Actual FD leak in the pipeline still needs a follow-up fix; this
# is the wrap that unblocks scheduled trading immediately.
ulimit -n 4096

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
