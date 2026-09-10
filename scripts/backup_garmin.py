"""Incrementally synchronize post-cutoff Garmin activities to SQLite."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select

from app.config import settings
from app.models.database import ActivityDB, GearDB, async_session, init_db
from app.models.strava import Activity
from app.services.garmin_service import GarminService

logger = logging.getLogger(__name__)


class GarminBackup:
    """Upsert Garmin activity summaries and their assigned gear."""

    def __init__(self, garmin: Optional[GarminService] = None) -> None:
        self.garmin = garmin or GarminService()
        self.stats = {
            "source": "garmin",
            "activities_saved": 0,
            "activities_updated": 0,
            "gear_saved": 0,
            "errors": [],
        }

    @staticmethod
    def _first_garmin_day() -> datetime:
        cutoff = datetime.strptime(settings.migration_cutoff, "%Y-%m-%d")
        return cutoff + timedelta(days=1)

    async def _sync_start(self) -> datetime:
        first_day = self._first_garmin_day()
        async with async_session() as session:
            latest = await session.scalar(
                select(func.max(ActivityDB.start_date)).where(
                    ActivityDB.source == "garmin"
                )
            )
        if latest is None:
            return first_day
        return max(
            first_day,
            latest - timedelta(days=settings.garmin_sync_lookback_days),
        )

    async def run(self) -> dict:
        await init_db()
        sync_start = await self._sync_start()
        logger.info("GARMIN SYNC TO SQLITE from %s", sync_start.isoformat())
        try:
            activities = await self.garmin.get_activities(
                after=sync_start,
                before=datetime.now(),
            )
            await self._upsert_activities(activities)
        except Exception as exc:
            self.stats["errors"].append(str(exc))
            raise
        logger.info("Garmin sync complete: %s", self.stats)
        return self.stats

    async def _upsert_activities(self, activities: list[Activity]) -> None:
        async with async_session() as session:
            for activity in activities:
                result = await session.execute(
                    select(ActivityDB).where(
                        ActivityDB.source == "garmin",
                        ActivityDB.source_id == str(activity.id),
                    )
                )
                existing = result.scalar_one_or_none()
                values = self._activity_values(activity)
                if existing:
                    for field, value in values.items():
                        setattr(existing, field, value)
                    self.stats["activities_updated"] += 1
                else:
                    session.add(
                        ActivityDB(
                            source="garmin",
                            source_id=str(activity.id),
                            **values,
                        )
                    )
                    self.stats["activities_saved"] += 1

                if activity.gear_id and activity.gear_name:
                    await self._upsert_gear(
                        session, activity.gear_id, activity.gear_name
                    )
            await session.commit()

    async def _upsert_gear(self, session, gear_id: str, gear_name: str) -> None:
        result = await session.execute(
            select(GearDB).where(
                GearDB.source == "garmin",
                GearDB.source_id == str(gear_id),
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.name = gear_name
        else:
            session.add(
                GearDB(
                    source="garmin",
                    source_id=str(gear_id),
                    name=gear_name,
                )
            )
            self.stats["gear_saved"] += 1

    @staticmethod
    def _activity_values(activity: Activity) -> dict:
        start_latlng = activity.start_latlng or []
        end_latlng = activity.end_latlng or []
        return {
            "name": activity.name,
            "sport_type": activity.sport_type,
            "activity_type": activity.type,
            "distance": activity.distance,
            "moving_time": activity.moving_time,
            "elapsed_time": activity.elapsed_time,
            "total_elevation_gain": activity.total_elevation_gain,
            "average_speed": activity.average_speed,
            "max_speed": activity.max_speed,
            "average_heartrate": activity.average_heartrate,
            "max_heartrate": activity.max_heartrate,
            "average_cadence": activity.average_cadence,
            "start_date": activity.start_date,
            "start_date_local": activity.start_date_local,
            "timezone": activity.timezone,
            "start_lat": start_latlng[0] if len(start_latlng) > 1 else None,
            "start_lng": start_latlng[1] if len(start_latlng) > 1 else None,
            "end_lat": end_latlng[0] if len(end_latlng) > 1 else None,
            "end_lng": end_latlng[1] if len(end_latlng) > 1 else None,
            "gear_id": activity.gear_id,
            "gear_name": activity.gear_name,
            "elev_high": activity.elev_high,
            "elev_low": activity.elev_low,
            "trainer": activity.trainer,
            "manual": activity.manual,
            "private": activity.private,
            "has_heartrate": activity.has_heartrate,
            "has_gps_data": bool(activity.start_latlng),
            "raw_data": activity.model_dump_json(),
        }


async def main() -> None:
    await GarminBackup().run()


if __name__ == "__main__":
    asyncio.run(main())
