#!/Users/ellingtonfagan/Downloads/nba-scenario-bot/.venv/bin/python3.14
"""Launch the scheduled demo cycle from cron.

WHY THIS IS PYTHON AND NOT BASH (despite the .sh name)
------------------------------------------------------
macOS TCC ("System Policy") grants filesystem access per *binary*, and
/bin/bash and /bin/zsh are NOT permitted to read files under ~/Downloads.
This repo lives in ~/Downloads, so a `#!/usr/bin/env bash` shebang made the
kernel hand the script to bash, bash was denied file-read-data on this very
file, and cron logged only:

    bash: .../scheduler/run-demo-cycle.sh: Operation not permitted

Confirmed in the unified log:
    kernel (Sandbox) System Policy: bash(55097) deny(1) file-read-data
        /Users/.../scheduler/run-demo-cycle.sh

Every scheduled job that kept working (news-watch, telegram-bot, monitor)
invokes .venv/bin/ksobot, whose shebang is the venv python -- and python IS
permitted to read ~/Downloads. So this entrypoint uses the same interpreter.
Silently broke the demo cycle 2026-08-12; no cycle ran for ~3 days.

The filename stays `run-demo-cycle.sh` because scheduler/combined-crontab.txt
(already installed via `crontab -`) hardcodes this exact path.
"""
from __future__ import annotations

import os
import resource
import sys

# Raise the file-descriptor soft limit before python starts the cycle. macOS
# cron/launchd inherit a 256 soft limit which was exhausted mid-cycle
# 2026-07-31 with `OSError: [Errno 24] Too many open files` after the
# 200-candidate matcher phase, so cycles ran ~90% of the way and then died
# before writing their summary. Actual FD leak in the pipeline still needs a
# follow-up fix; this is the wrap that unblocks scheduled trading immediately.
FD_LIMIT = 4096

REPO = os.environ.get("NBABOT_REPO") or "/Users/ellingtonfagan/Downloads/nba-scenario-bot"
KSOBOT = os.environ.get("KSOBOT") or os.path.join(REPO, ".venv/bin/ksobot")


def _raise_fd_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = FD_LIMIT if hard == resource.RLIM_INFINITY else min(FD_LIMIT, hard)
    if soft >= target:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as exc:
        print(f"[run-demo-cycle] could not raise FD limit to {target}: {exc}", file=sys.stderr)


def main() -> int:
    _raise_fd_limit()

    try:
        os.chdir(REPO)
    except OSError as exc:
        print(f"[run-demo-cycle] cannot cd to {REPO}: {exc}", file=sys.stderr)
        return 2

    os.makedirs(os.environ.get("NBABOT_LOG_DIR") or os.path.join(REPO, "logs"), exist_ok=True)

    os.environ["NBABOT_EXECUTION_MODE"] = "demo"
    os.environ.setdefault("NBABOT_DELIVER_TO", "telegram")

    if not os.access(KSOBOT, os.X_OK):
        print(f"[run-demo-cycle] missing executable: {KSOBOT}", file=sys.stderr)
        return 127

    os.execv(KSOBOT, [KSOBOT, "scheduled-demo-cycle", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
