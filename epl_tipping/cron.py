from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .football_data import FootballDataConfig, sync_matches_once
from .runner import RunnerConfig, run_due_once
from .simulation import SimulationConfig, process_next_simulation
from .storage import get_store


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--competition", default=None)
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--source-timeout-seconds", type=float, default=None)


def source_config_from_args(args: argparse.Namespace) -> FootballDataConfig:
    env_config = FootballDataConfig.from_env()
    return FootballDataConfig(
        token=env_config.token,
        base_url=env_config.base_url,
        competition_code=args.competition or env_config.competition_code,
        season=args.season or env_config.season,
        timeout_seconds=args.source_timeout_seconds or env_config.timeout_seconds,
        retries=env_config.retries,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="EPL tipping workflows")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-due", help="Sync fixtures, collect due tips, and score results")
    run_parser.add_argument("--data-dir", type=Path)
    run_parser.add_argument("--lock-minutes", type=int, default=30)
    run_parser.add_argument("--lookahead-hours", type=int, default=24)
    run_parser.add_argument("--timeout-seconds", type=float, default=15.0)
    run_parser.add_argument("--retries", type=int, default=1)
    run_parser.add_argument("--concurrency", type=int, default=20)
    run_parser.add_argument("--no-sync-source", action="store_true")
    add_source_args(run_parser)

    sync_parser = subparsers.add_parser("sync-fixtures", help="Synchronise fixtures and results")
    sync_parser.add_argument("--data-dir", type=Path)
    sync_parser.add_argument("--dry-run", action="store_true")
    add_source_args(sync_parser)

    simulation_parser = subparsers.add_parser(
        "process-simulation",
        help="Process the next queued season simulation",
    )
    simulation_parser.add_argument("--data-dir", type=Path)
    simulation_parser.add_argument("--timeout-seconds", type=float, default=15.0)
    simulation_parser.add_argument("--retries", type=int, default=1)
    simulation_parser.add_argument("--concurrency", type=int, default=5)

    args = parser.parse_args()
    store = get_store(args.data_dir)

    if args.command == "run-due":
        result = asyncio.run(
            run_due_once(
                store,
                RunnerConfig(
                    lock_minutes=args.lock_minutes,
                    lookahead_hours=args.lookahead_hours,
                    timeout_seconds=args.timeout_seconds,
                    retries=args.retries,
                    concurrency=args.concurrency,
                    sync_source=not args.no_sync_source,
                ),
                source_config=source_config_from_args(args),
            )
        )
    elif args.command == "sync-fixtures":
        result = asyncio.run(
            sync_matches_once(
                store,
                config=source_config_from_args(args),
                dry_run=args.dry_run,
            )
        )
    else:
        result = asyncio.run(
            process_next_simulation(
                store,
                SimulationConfig(
                    timeout_seconds=args.timeout_seconds,
                    retries=args.retries,
                    concurrency=args.concurrency,
                ),
            )
        )
        if result is None:
            result = {"status": "idle", "message": "No queued season simulation"}

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
