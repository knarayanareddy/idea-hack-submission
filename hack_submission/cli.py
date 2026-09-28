"""Command-line interface for the Hack submission campaign engine."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .engine import (
    ArchiveIndex,
    CampaignEngine,
    JsonStateStore,
    atomic_write_text,
    validate_submission,
)
from .models import (
    UTC,
    CampaignPlan,
    CampaignRequest,
    DeploymentChannel,
    HackSubmission,
    ModelValidationError,
    build_muse_submission,
    parse_datetime,
)


_CHANNEL_CHOICES = [
    channel.value
    for channel in DeploymentChannel
    if channel is not DeploymentChannel.LOCAL
]


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _write_json(path: Path, value: Any) -> None:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    atomic_write_text(path, serialized, mode=0o644)


def _read_json(path: Path) -> dict[str, Any]:
    if str(path) == "-":
        try:
            value = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on standard input: {exc.msg}") from exc
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc.msg}") from exc

    if not isinstance(value, dict):
        raise ValueError(f"JSON document in {path} must contain an object")
    return value


def _load_submission(path: Path | None) -> HackSubmission:
    if path is None:
        return build_muse_submission()
    return HackSubmission.from_dict(_read_json(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hack-submission",
        description=(
            "Index the Hackathon 2025 finalist archive and execute timed "
            "visibility campaigns for the Muse submission."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    muse_data = commands.add_parser(
        "muse-data",
        aliases=["demo"],
        help="write the built-in Muse reference submission",
    )
    muse_data.add_argument("--output", type=Path)

    validate = commands.add_parser(
        "validate",
        help="validate a HackSubmission JSON document",
    )
    validate.add_argument("--file", "-f", type=Path)
    validate.add_argument("--output", type=Path)

    index = commands.add_parser(
        "index",
        help="search the ranked finalist archive",
    )
    index.add_argument("--file", "-f", type=Path)
    index.add_argument("--query", default="")
    index.add_argument("--track")
    index.add_argument("--limit", type=int, default=10)

    plan = commands.add_parser(
        "plan",
        help="create a chronological visibility campaign plan",
    )
    plan.add_argument("--file", "-f", type=Path)
    plan.add_argument(
        "--launch-at",
        help="ISO-8601 launch time; defaults to 48 hours from now",
    )
    plan.add_argument(
        "--channels",
        nargs="+",
        choices=_CHANNEL_CHOICES,
        default=list(_CHANNEL_CHOICES),
    )
    plan.add_argument("--output", type=Path)
    plan.add_argument("--state", type=Path)

    execute = commands.add_parser(
        "execute",
        help="execute all due tasks in a campaign plan",
    )
    execute.add_argument("--plan", "-p", type=Path, required=True)
    execute.add_argument("--output-dir", "-o", type=Path, required=True)
    execute.add_argument(
        "--state",
        type=Path,
        default=Path(".hack-submission/state.json"),
    )
    execute.add_argument(
        "--now",
        help="ISO-8601 execution time; defaults to the current UTC time",
    )
    execute.add_argument(
        "--allow-early",
        action="store_true",
        help="execute future tasks immediately for a controlled dry run",
    )

    status = commands.add_parser(
        "status",
        help="inspect persisted campaign state",
    )
    status.add_argument(
        "--state",
        type=Path,
        default=Path(".hack-submission/state.json"),
    )
    status.add_argument("--plan-id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command in {"muse-data", "demo"}:
            submission = build_muse_submission()
            if args.output:
                _write_json(args.output, submission.to_dict())
                _print_json(
                    {
                        "written": str(args.output),
                        "submission_slug": submission.slug,
                        "team_size": len(submission.team),
                        "finalist_count": len(submission.top_finalists),
                    }
                )
            else:
                _print_json(submission.to_dict())
            return 0

        if args.command == "validate":
            submission = _load_submission(args.file)
            result = validate_submission(submission)
            if args.output:
                _write_json(args.output, result)
            _print_json(result)
            return 0

        if args.command == "index":
            submission = _load_submission(args.file)
            archive = ArchiveIndex.from_submission(submission)
            hits = archive.search(
                query=args.query,
                track=args.track,
                limit=args.limit,
            )
            result = {
                "query": args.query,
                "track": args.track,
                "statistics": archive.statistics().to_dict(),
                "results": [hit.to_dict() for hit in hits],
            }
            _print_json(result)
            return 0

        if args.command == "plan":
            submission = _load_submission(args.file)
            launch_at = (
                parse_datetime(args.launch_at, "--launch-at")
                if args.launch_at
                else datetime.now(UTC) + timedelta(hours=48)
            )
            request = CampaignRequest(
                launch_at=launch_at,
                channels=tuple(args.channels),
            )
            store = JsonStateStore(args.state) if args.state else None
            engine = CampaignEngine(store=store)
            campaign_plan = engine.create_plan(submission, request)
            if args.state:
                engine.save_plan(campaign_plan)
            if args.output:
                _write_json(args.output, campaign_plan.to_dict())
            _print_json(campaign_plan.to_dict())
            return 0

        if args.command == "execute":
            plan = CampaignPlan.from_dict(_read_json(args.plan))
            now = parse_datetime(args.now, "--now") if args.now else None
            engine = CampaignEngine(store=JsonStateStore(args.state))
            report = engine.execute(
                plan,
                args.output_dir,
                now=now,
                allow_early=args.allow_early,
            )
            _print_json(report.to_dict())
            return 0 if report.failed_count == 0 else 1

        if args.command == "status":
            store = JsonStateStore(args.state)
            _print_json(store.status(args.plan_id))
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except (ModelValidationError, ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run() -> None:
    """Console-script entry point."""

    raise SystemExit(main())


__all__ = ["build_parser", "main", "run"]
