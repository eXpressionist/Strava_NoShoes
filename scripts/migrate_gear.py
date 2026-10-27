"""Run with python -m scripts.migrate_gear; dry-run unless --apply is specified."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.gear_migration_service import GearMigration


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


@contextmanager
def migration_lock():
    """OS releases the lock even if the process crashes during a request."""
    path = Path(settings.garmin_token_store) / "gear-migration.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(ZoneInfo(args.timezone)).date()
    first, last = args.from_date, args.to_date or today
    if first is not None and first > last:
        raise ValueError("--from-date must not be later than --to-date")
    migration = GearMigration(delay=args.request_delay)
    if not settings.garmin_email or not settings.garmin_password:
        raise ValueError(
            "Configure GARMIN_EMAIL and GARMIN_PASSWORD in the primary container"
        )
    mapping = (
        json.loads(args.gear_map.read_text(encoding="utf-8")) if args.gear_map else {}
    )
    if not isinstance(mapping, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()
    ):
        raise ValueError(
            "--gear-map must contain a JSON object of Strava IDs to Garmin UUIDs"
        )
    print(
        f"Matching archived Strava and Garmin activities: {first or 'first Garmin activity'} through {last} ({args.timezone})",
        flush=True,
    )
    report = await migration.build_plan(first, last, args.timezone, mapping)
    first = date.fromisoformat(report["from_date"])
    print(f"Selected period: {first} through {last} ({args.timezone})", flush=True)
    report["mode"] = "apply" if args.apply else "dry_run"
    report["replace_existing"] = args.replace_existing
    write_report(args.report, report)
    print(
        f"Strava SQLite backup coverage: {report['archive']['first_activity_utc']} through {report['archive']['latest_activity_utc']} UTC",
        flush=True,
    )
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"Report: {args.report}", flush=True)
    if not args.apply:
        print("Dry run complete; no Garmin gear or activity assignments changed.")
        return 0
    args.journal.parent.mkdir(parents=True, exist_ok=True)

    def journal(event: dict) -> None:
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "strava_athlete_id": report["strava_athlete_id"],
            **event,
        }
        with args.journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    journal(
        {
            "event": "start",
            "from_date": first.isoformat(),
            "to_date": last.isoformat(),
            "replace_existing": args.replace_existing,
        }
    )
    try:
        report["result"] = await migration.apply(report, journal, args.replace_existing)
    except Exception as exc:
        # Do not put API response bodies or credentials into the audit trail.
        report["error"] = type(exc).__name__
        journal({"event": "failed", "error": type(exc).__name__})
        write_report(args.report, report)
        raise
    journal({"event": "complete", "result": report["result"]})
    write_report(args.report, report)
    print(json.dumps(report["result"], ensure_ascii=False), flush=True)
    print(f"Journal: {args.journal}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-date",
        type=date.fromisoformat,
        help="First date; defaults to the first Garmin activity",
    )
    parser.add_argument("--to-date", type=date.fromisoformat)
    parser.add_argument("--timezone", default="Europe/Moscow")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create gear and assign it to uniquely matched activities",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="With --apply, replace other gear of the same category; otherwise report conflicts",
    )
    parser.add_argument(
        "--gear-map",
        type=Path,
        help="Optional JSON mapping Strava gear IDs to existing Garmin UUIDs",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("data/gear-migration-report.json")
    )
    parser.add_argument(
        "--journal", type=Path, default=Path("data/gear-migration-journal.jsonl")
    )
    parser.add_argument("--request-delay", type=float, default=0.5)
    args = parser.parse_args()
    if not 0 <= args.request_delay <= 60:
        parser.error("--request-delay must be between 0 and 60 seconds")
    try:
        with migration_lock():
            return asyncio.run(run(args))
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(
            f"Migration stopped: {detail}. Check credentials, report and journal before rerunning.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
