"""
Unified Activity Service
=========================
Combines data from two sources:
- SQLite (Strava backup) — for activities before MIGRATION_CUTOFF
- Garmin Connect API — for activities after MIGRATION_CUTOFF

The bot and REST API use this service exclusively.
"""

import json
import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, func

from app.config import settings
from app.models.database import (
    ActivityDB, GearDB, ActivityStreamDB,
    async_session, init_db
)
from app.models.strava import Activity, Gear, ActivityFilter
from app.services.garmin_service import GarminService, GarminAPIError

import gpxpy
import gpxpy.gpx
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class UnifiedServiceError(Exception):
    pass


class UnifiedActivityService:
    """
    Single entry point for activity data.
    Routes requests to SQLite or Garmin based on date.
    """

    def __init__(self):
        self.garmin = GarminService()
        self._cutoff = datetime.strptime(settings.migration_cutoff, "%Y-%m-%d")
        self._db_initialized = False

    async def _ensure_db(self):
        if not self._db_initialized:
            await init_db()
            self._db_initialized = True

    # ─── Activities ────────────────────────────────────────────────────

    async def get_activities(
        self,
        activity_filter: Optional[ActivityFilter] = None,
        all_pages: bool = False,
    ) -> List[Activity]:
        """Get activities from both sources, merged and sorted."""
        await self._ensure_db()

        after = activity_filter.after if activity_filter else None
        before = activity_filter.before if activity_filter else None

        results: List[Activity] = []

        # Determine which sources to query
        need_sqlite = (after is None or after < self._cutoff)
        need_garmin = (before is None or before > self._cutoff)

        if need_sqlite:
            sqlite_activities = await self._get_activities_from_db(activity_filter)
            results.extend(sqlite_activities)

        if need_garmin:
            garmin_after = max(after, self._cutoff) if after else self._cutoff
            garmin_before = before
            try:
                garmin_activities = await self.garmin.get_activities(
                    after=garmin_after,
                    before=garmin_before,
                )
                # Apply additional filters
                if activity_filter:
                    garmin_activities = self._apply_filters(garmin_activities, activity_filter)
                results.extend(garmin_activities)
            except GarminAPIError as e:
                logger.warning(f"Garmin API error: {e}. Returning only SQLite data.")

        # Sort newest first
        results.sort(key=lambda x: x.start_date, reverse=True)
        return results

    async def get_activity_by_id(self, activity_id: int, source: Optional[str] = None) -> Activity:
        """Get activity by ID. Try SQLite first, then Garmin."""
        await self._ensure_db()

        db_activity = None
        if source in (None, "strava"):
            async with async_session() as session:
                query = select(ActivityDB).where(ActivityDB.source_id == str(activity_id))
                if source:
                    query = query.where(ActivityDB.source == source)
                result = await session.execute(query)
                db_activity = result.scalar_one_or_none()

        if db_activity:
            return self._db_to_model(db_activity)

        if source == "strava":
            raise UnifiedServiceError(f"Strava activity {activity_id} was not found")

        # Try Garmin
        try:
            return await self.garmin.get_activity_by_id(activity_id)
        except GarminAPIError as e:
            raise UnifiedServiceError(f"Activity {activity_id} not found in any source: {e}")

    async def get_activities_without_gear(self, after: Optional[datetime] = None) -> List[Activity]:
        """Get activities without gear from both sources."""
        activity_filter = ActivityFilter(has_gear=False, after=after)
        return await self.get_activities(activity_filter, all_pages=True)

    async def get_running_activities(self, limit: Optional[int] = None) -> List[Activity]:
        """Get running activities from both sources."""
        activity_filter = ActivityFilter(activity_type="Run")
        activities = await self.get_activities(activity_filter, all_pages=True)
        if limit:
            return activities[:limit]
        return activities

    # ─── Gear ──────────────────────────────────────────────────────────

    async def get_athlete_gear(self) -> List[Gear]:
        """Get gear from both SQLite and Garmin."""
        await self._ensure_db()
        gear_list: List[Gear] = []

        # SQLite gear
        async with async_session() as session:
            result = await session.execute(select(GearDB))
            for g in result.scalars().all():
                gear_list.append(Gear(
                    id=g.source_id,
                    primary=g.primary,
                    name=g.name,
                    resource_state=2,
                    retired=g.retired,
                    distance=g.distance,
                ))

        # Garmin gear (may overlap, deduplicate by name)
        try:
            garmin_gear = await self.garmin.get_athlete_gear()
            existing_names = {g.name for g in gear_list}
            for g in garmin_gear:
                if g.name not in existing_names:
                    gear_list.append(g)
        except GarminAPIError as e:
            logger.warning(f"Could not fetch Garmin gear: {e}")

        return gear_list

    # ─── GPX ──────────────────────────────────────────────────────────

    async def download_gpx(self, activity_id: int, save_path: Optional[str] = None, activity_name: Optional[str] = None) -> str:
        """Download/generate GPX. Try Garmin first (native GPX), fall back to SQLite streams."""
        if not save_path:
            save_path = settings.gpx_storage_path
        Path(save_path).mkdir(parents=True, exist_ok=True)

        await self._ensure_db()

        # Check if this is a Garmin activity
        async with async_session() as session:
            result = await session.execute(
                select(ActivityDB).where(ActivityDB.source_id == str(activity_id))
            )
            db_activity = result.scalar_one_or_none()

        if db_activity and db_activity.source == "strava":
            # Generate GPX from stored streams
            return await self._gpx_from_streams(db_activity, save_path, activity_name)
        else:
            # Try Garmin native GPX download
            try:
                return await self.garmin.download_gpx(activity_id, save_path, activity_name)
            except GarminAPIError:
                # Maybe it's in SQLite after all
                if db_activity:
                    return await self._gpx_from_streams(db_activity, save_path, activity_name)
                raise UnifiedServiceError(f"Cannot download GPX for activity {activity_id}")

    # ─── Private helpers ───────────────────────────────────────────────

    async def _get_activities_from_db(self, activity_filter: Optional[ActivityFilter]) -> List[Activity]:
        """Query SQLite for activities."""
        async with async_session() as session:
            query = select(ActivityDB)

            if activity_filter:
                if activity_filter.after:
                    query = query.where(ActivityDB.start_date > activity_filter.after)
                if activity_filter.before:
                    query = query.where(ActivityDB.start_date < activity_filter.before)
                else:
                    # Don't return activities past cutoff from SQLite
                    query = query.where(ActivityDB.start_date <= self._cutoff)

                if activity_filter.activity_type:
                    query = query.where(ActivityDB.sport_type == activity_filter.activity_type)
                if activity_filter.has_gear is not None:
                    if activity_filter.has_gear:
                        query = query.where(ActivityDB.gear_id != None)
                    else:
                        query = query.where(ActivityDB.gear_id == None)
                if activity_filter.gear_id:
                    query = query.where(ActivityDB.gear_id == activity_filter.gear_id)
            else:
                query = query.where(ActivityDB.start_date <= self._cutoff)

            query = query.order_by(ActivityDB.start_date.desc())
            result = await session.execute(query)
            return [self._db_to_model(a) for a in result.scalars().all()]

    def _db_to_model(self, db: ActivityDB) -> Activity:
        """Convert DB record to Pydantic Activity model."""
        workout_type = None
        if db.raw_data:
            try:
                workout_type = json.loads(db.raw_data).get("workout_type")
            except (json.JSONDecodeError, TypeError):
                pass
        return Activity(
            source=db.source,
            resource_state=2,
            athlete=None,
            name=db.name,
            distance=db.distance or 0.0,
            moving_time=db.moving_time or 0,
            elapsed_time=db.elapsed_time or 0,
            total_elevation_gain=db.total_elevation_gain or 0.0,
            type=db.activity_type or db.sport_type,
            sport_type=db.sport_type,
            workout_type=workout_type,
            id=int(db.source_id),
            start_date=db.start_date,
            start_date_local=db.start_date_local,
            timezone=db.timezone or "",
            utc_offset=0.0,
            achievement_count=0,
            kudos_count=0,
            comment_count=0,
            athlete_count=1,
            photo_count=0,
            trainer=db.trainer or False,
            commute=False,
            manual=db.manual or False,
            private=db.private or False,
            visibility="everyone",
            flagged=False,
            gear_id=db.gear_id,
            gear_name=db.gear_name,
            start_latlng=[db.start_lat, db.start_lng] if db.start_lat else None,
            end_latlng=[db.end_lat, db.end_lng] if db.end_lat else None,
            average_speed=db.average_speed or 0.0,
            max_speed=db.max_speed or 0.0,
            average_cadence=db.average_cadence,
            has_heartrate=db.has_heartrate or False,
            average_heartrate=db.average_heartrate,
            max_heartrate=db.max_heartrate,
            elev_high=db.elev_high,
            elev_low=db.elev_low,
            pr_count=0,
            total_photo_count=0,
            has_kudoed=False,
        )

    def _apply_filters(self, activities: List[Activity], f: ActivityFilter) -> List[Activity]:
        """Apply ActivityFilter to a list of activities."""
        result = activities
        if f.activity_type:
            result = [a for a in result if a.sport_type == f.activity_type]
        if f.has_gear is not None:
            if f.has_gear:
                result = [a for a in result if a.gear_id]
            else:
                result = [a for a in result if not a.gear_id]
        if f.gear_id:
            result = [a for a in result if a.gear_id == f.gear_id]
        return result

    async def _gpx_from_streams(self, db_activity: ActivityDB, save_path: str, activity_name: Optional[str]) -> str:
        """Generate GPX file from stored stream data in SQLite."""
        name = activity_name or db_activity.name
        safe_name = name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        filename = f"{safe_name}.gpx"
        file_path = os.path.join(save_path, filename)

        if os.path.exists(file_path):
            return file_path

        async with async_session() as session:
            result = await session.execute(
                select(ActivityStreamDB)
                .where(ActivityStreamDB.activity_id == db_activity.id)
                .order_by(ActivityStreamDB.point_index)
            )
            points = result.scalars().all()

        if not points:
            raise UnifiedServiceError(f"No stream data stored for activity {db_activity.source_id}")

        # Build GPX
        gpx = gpxpy.gpx.GPX()
        gpx.name = name
        gpx.description = f"Activity {db_activity.source_id} (from Strava backup)"

        track = gpxpy.gpx.GPXTrack()
        gpx.tracks.append(track)

        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)

        start_time = db_activity.start_date

        for pt in points:
            if pt.latitude is None or pt.longitude is None:
                continue

            from datetime import timedelta
            point_time = start_time + timedelta(seconds=pt.time_offset) if pt.time_offset else None

            gpx_point = gpxpy.gpx.GPXTrackPoint(
                latitude=pt.latitude,
                longitude=pt.longitude,
                elevation=pt.altitude,
                time=point_time,
            )

            # Add HR/cadence extensions
            if pt.heartrate or pt.cadence:
                from lxml import etree
                TPE_NS = 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1'
                tpx = etree.Element(f'{{{TPE_NS}}}TrackPointExtension')
                if pt.heartrate:
                    hr = etree.SubElement(tpx, f'{{{TPE_NS}}}hr')
                    hr.text = str(pt.heartrate)
                if pt.cadence:
                    cad = etree.SubElement(tpx, f'{{{TPE_NS}}}cad')
                    cad.text = str(pt.cadence)
                gpx_point.extensions.append(tpx)

            segment.points.append(gpx_point)

        # Write GPX
        gpx_xml = gpx.to_xml()
        TPE_NS = 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1'
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:TrackPointExtension>', '<gpxtpx:TrackPointExtension>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:TrackPointExtension>', '</gpxtpx:TrackPointExtension>')
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:hr>', '<gpxtpx:hr>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:hr>', '</gpxtpx:hr>')
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:cad>', '<gpxtpx:cad>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:cad>', '</gpxtpx:cad>')
        if 'xmlns:gpxtpx' not in gpx_xml:
            gpx_xml = gpx_xml.replace('<gpx ', f'<gpx xmlns:gpxtpx="{TPE_NS}" ')

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(gpx_xml)

        return file_path
