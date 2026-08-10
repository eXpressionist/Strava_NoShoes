"""Automatic, resumable Strava backup scheduling."""

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import exists, func, select

from app.config import settings
from app.models.database import (
    ActivityDB,
    ActivityStreamDB,
    GearDB,
    async_session,
    init_db,
)
from scripts.backup_strava import StravaBackup

logger = logging.getLogger(__name__)


class StravaBackupScheduler:
    """Run one backup at a time until the configured inclusive cutoff day."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self._lock = asyncio.Lock()
        self._startup_task: Optional[asyncio.Task] = None
        self.last_started_at: Optional[datetime] = None
        self.last_finished_at: Optional[datetime] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    @staticmethod
    def _cutoff() -> date:
        return datetime.strptime(settings.migration_cutoff, "%Y-%m-%d").date()

    def should_run(self, today: Optional[date] = None) -> bool:
        return (
            settings.strava_backup_enabled and (today or date.today()) <= self._cutoff()
        )

    async def run_backup(self) -> Optional[dict]:
        if not self.should_run():
            logger.info("Automatic Strava backup is outside its active date window")
            return None
        if self._lock.locked():
            logger.info("A Strava backup is already running; duplicate run skipped")
            return None

        async with self._lock:
            self.last_started_at = datetime.now()
            self.last_error = None
            try:
                self.last_result = await StravaBackup().run()
                return self.last_result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("Automatic Strava backup failed; next run will resume")
                return None
            finally:
                self.last_finished_at = datetime.now()

    async def status(self) -> dict:
        await init_db()
        async with async_session() as session:
            total = await session.scalar(
                select(func.count(ActivityDB.id)).where(ActivityDB.source == "strava")
            )
            latest = await session.scalar(
                select(func.max(ActivityDB.start_date)).where(
                    ActivityDB.source == "strava"
                )
            )
            gear = await session.scalar(
                select(func.count(GearDB.id)).where(GearDB.source == "strava")
            )
            streams = await session.scalar(
                select(func.count(func.distinct(ActivityStreamDB.activity_id)))
            )
            pending_streams = await session.scalar(
                select(func.count(ActivityDB.id)).where(
                    ActivityDB.source == "strava",
                    ActivityDB.has_gps_data == True,
                    ~exists().where(ActivityStreamDB.activity_id == ActivityDB.id),
                )
            )

        return {
            "enabled": settings.strava_backup_enabled,
            "active_through": self._cutoff().isoformat(),
            "running": self._lock.locked(),
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "database": {
                "activities": total or 0,
                "latest_activity_at": latest,
                "gear": gear or 0,
                "activities_with_streams": streams or 0,
                "activities_pending_streams": pending_streams or 0,
            },
        }

    async def start(self) -> None:
        if not self.should_run():
            return

        self.scheduler.add_job(
            self.run_backup,
            CronTrigger(
                hour=settings.strava_backup_schedule_hour,
                minute=settings.strava_backup_schedule_minute,
            ),
            id="strava_backup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        logger.info(
            "Daily Strava backup scheduled for %02d:%02d through %s",
            settings.strava_backup_schedule_hour,
            settings.strava_backup_schedule_minute,
            self._cutoff(),
        )
        if settings.strava_backup_on_startup:
            self._startup_task = asyncio.create_task(
                self.run_backup(), name="strava-backup-on-startup"
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


backup_scheduler = StravaBackupScheduler()
