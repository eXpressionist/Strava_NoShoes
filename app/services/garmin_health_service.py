"""Resumable collection of Garmin wellness data for future dashboards."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import Integer, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import GarminHealthDB, async_session, init_db
from app.services.garmin_service import GarminService

logger = logging.getLogger(__name__)

# Only read methods accepting one local calendar date. Keep original responses:
# units, nested fields and intraday series can be interpreted by future dashboards.
HEALTH_METRICS = {
    "daily_summary": "get_user_summary",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "body_battery": "get_body_battery",
    "stress": "get_stress_data",
    "heart_rate": "get_heart_rates",
    "resting_heart_rate": "get_rhr_day",
    "steps": "get_steps_data",
    "spo2": "get_spo2_data",
    "respiration": "get_respiration_data",
    "training_readiness": "get_training_readiness",
    "training_status": "get_training_status",
    "max_metrics": "get_max_metrics",
    "body_composition": "get_body_composition",
    "hydration": "get_hydration_data",
}


def _http_status(exc: Exception) -> Optional[int]:
    """Garmin versions may attach the response to a wrapped exception."""
    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status is not None:
            return status
        current = current.__cause__
    return None


class GarminHealthSync:
    """Refresh recent days and backfill missing history separately per metric."""

    def __init__(self, garmin: Optional[GarminService] = None) -> None:
        self.garmin = garmin or GarminService()

    async def _dates(self, metric: str, today: date) -> list[date]:
        first_day = (
            date.fromisoformat(settings.garmin_health_start_date)
            if settings.garmin_health_start_date
            else today - timedelta(days=settings.garmin_health_history_days - 1)
        )
        recent_start = max(
            first_day, today - timedelta(days=settings.garmin_health_lookback_days - 1)
        )
        recent = [
            recent_start + timedelta(days=offset)
            for offset in range(max(0, (today - recent_start).days + 1))
        ]
        async with async_session() as session:
            existing = set(
                (
                    await session.scalars(
                        select(GarminHealthDB.calendar_date).where(
                            GarminHealthDB.metric == metric,
                            GarminHealthDB.calendar_date >= first_day,
                            GarminHealthDB.calendar_date < recent_start,
                        )
                    )
                ).all()
            )
        missing = []
        day = first_day
        while day < recent_start:
            if day not in existing:
                missing.append(day)
                if len(missing) >= settings.garmin_health_backfill_days_per_run:
                    break
            day += timedelta(days=1)
        # Recent data comes first; one unavailable sensor never blocks the
        # historical progress of other sensors.
        return list(reversed(recent)) + missing

    async def _save(self, metric: str, day: date, payload: Any) -> None:
        statement = insert(GarminHealthDB).values(
            metric=metric,
            calendar_date=day,
            payload=payload,
            has_data=bool(payload),
            fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        statement = statement.on_conflict_do_update(
            index_elements=["metric", "calendar_date"],
            set_={
                "payload": statement.excluded.payload,
                "has_data": statement.excluded.has_data,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
        # Commit each response so cancellation or an API outage loses no progress.
        async with async_session() as session:
            await session.execute(statement)
            await session.commit()

    async def run(self, today: Optional[date] = None) -> dict:
        await init_db()
        today = today or datetime.now(ZoneInfo(settings.garmin_health_timezone)).date()
        result: dict[str, Any] = {
            "records_saved": 0,
            "empty_responses": 0,
            "errors": [],
        }
        try:
            await self.garmin._ensure_connected()
        except Exception as exc:
            # Exception messages may contain private URLs or response bodies.
            result["errors"].append({"error": type(exc).__name__})
            return result

        for metric, method_name in HEALTH_METRICS.items():
            method = getattr(self.garmin.client, method_name, None)
            if method is None:
                result["errors"].append({"metric": metric, "error": "unsupported"})
                continue
            for day in await self._dates(metric, today):
                await asyncio.sleep(settings.garmin_health_request_delay_seconds)
                try:
                    payload = await asyncio.to_thread(method, day.isoformat())
                except Exception as exc:
                    error_type = type(exc).__name__
                    status = _http_status(exc)
                    if status == 404 or error_type == "GarminConnectNotFoundError":
                        await self._save(metric, day, None)
                        result["records_saved"] += 1
                        result["empty_responses"] += 1
                        continue
                    result["errors"].append(
                        {"metric": metric, "date": day.isoformat(), "error": error_type}
                    )
                    logger.warning(
                        "Garmin health %s on %s: %s", metric, day, error_type
                    )
                    if (
                        status in {401, 429}
                        or error_type
                        in {
                            "GarminConnectAuthenticationError",
                            "GarminConnectTooManyRequestsError",
                        }
                        or (
                            error_type == "GarminConnectConnectionError"
                            and status is None
                        )
                    ):
                        return result
                    # Retry next run, leaving any previously saved response intact.
                    # Skip the rest of this metric to avoid hammering an endpoint
                    # that the account or watch does not support.
                    break
                await self._save(metric, day, payload)
                result["records_saved"] += 1
                result["empty_responses"] += int(not payload)
        return result


class GarminHealthScheduler:
    """An independent health job; activity sync failures cannot prevent it."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._startup_task: Optional[asyncio.Task] = None
        self.last_started_at: Optional[datetime] = None
        self.last_finished_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    @staticmethod
    def is_configured() -> bool:
        return (
            settings.garmin_health_sync_enabled
            and settings.migration_enabled
            and bool(settings.garmin_email and settings.garmin_password)
        )

    @classmethod
    def should_run(cls) -> bool:
        today = datetime.now(ZoneInfo(settings.garmin_health_timezone)).date()
        return cls.is_configured() and today > date.fromisoformat(
            settings.migration_cutoff
        )

    async def run_sync(self) -> Optional[dict]:
        if not self.should_run() or self._lock.locked():
            return None
        async with self._lock:
            self.last_started_at = datetime.now(timezone.utc)
            self.last_error = None
            try:
                self.last_result = await GarminHealthSync().run()
                if self.last_result["errors"]:
                    self.last_error = (
                        "Some Garmin health requests failed; see last_result"
                    )
                return self.last_result
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.warning("Garmin health sync failed: %s", self.last_error)
                return None
            finally:
                self.last_finished_at = datetime.now(timezone.utc)

    async def status(self, session: AsyncSession) -> dict:
        rows = (
            await session.execute(
                select(
                    GarminHealthDB.metric,
                    func.count(GarminHealthDB.id),
                    func.min(GarminHealthDB.calendar_date),
                    func.max(GarminHealthDB.calendar_date),
                    func.max(GarminHealthDB.fetched_at),
                    func.sum(GarminHealthDB.has_data.cast(Integer)),
                ).group_by(GarminHealthDB.metric)
            )
        ).all()
        return {
            "enabled": settings.garmin_health_sync_enabled,
            "active": self.should_run(),
            "running": self._lock.locked(),
            "timezone": settings.garmin_health_timezone,
            "interval_hours": settings.garmin_health_sync_interval_hours,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "metrics": {
                metric: {
                    "records": count,
                    "days_with_data": with_data or 0,
                    "first_date": first,
                    "latest_date": latest,
                    "last_fetched_at": fetched,
                }
                for metric, count, first, latest, fetched, with_data in rows
            },
        }

    async def start(self) -> None:
        if not self.is_configured():
            return
        # Prepare schema before starting either background synchronization.
        # Concurrent create_all calls on a new SQLite database can race.
        await init_db()
        self.scheduler.add_job(
            self.run_sync,
            "interval",
            hours=settings.garmin_health_sync_interval_hours,
            id="garmin_health",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        self._startup_task = asyncio.create_task(self.run_sync(), name="garmin-health")
        logger.info(
            "Garmin health sync scheduled every %s hours (%s)",
            settings.garmin_health_sync_interval_hours,
            settings.garmin_health_timezone,
        )

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass


garmin_health_scheduler = GarminHealthScheduler()
