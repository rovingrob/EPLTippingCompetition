from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from .models import parse_iso_z
from .runner import RunnerConfig, run_due_once
from .storage import get_store


def main() -> None:
    parser = argparse.ArgumentParser(description="World Cup tipping cron workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_due = subparsers.add_parser("run-due", help="Call due endpoints and score completed fixtures.")
    run_due.add_argument("--data-dir", type=Path, default=None)
    run_due.add_argument("--now", default=None, help="UTC ISO timestamp override, for tests or dry runs.")
    run_due.add_argument("--lock-minutes", type=int, default=30)
    run_due.add_argument("--lookahead-hours", type=int, default=24)
    run_due.add_argument("--timeout-seconds", type=float, default=15.0)
    run_due.add_argument("--retries", type=int, default=1)
    args = parser.parse_args()

    if args.command == "run-due":
        now: datetime | None = parse_iso_z(args.now) if args.now else None
        config = RunnerConfig(
            lock_minutes=args.lock_minutes,
            lookahead_hours=args.lookahead_hours,
            timeout_seconds=args.timeout_seconds,
            retries=args.retries,
        )
        result = asyncio.run(run_due_once(get_store(args.data_dir), config, now))
        print(result)


if __name__ == "__main__":
    main()
