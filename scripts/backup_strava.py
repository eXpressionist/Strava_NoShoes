"""
Strava Backup Script
====================
Pulls ALL activities from Strava API (before it shuts down) and stores them in SQLite.
Also downloads streams (GPS/HR/cadence) for GPX reconstruction.

Usage:
    python -m scripts.backup_strava

Run this BEFORE Strava API becomes unavailable (deadline: June 30, 2025).
"""

import asyncio
import json
import logging
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.config import settings
from app.models.database import (
    ActivityDB, GearDB, ActivityStreamDB,
    engine, async_session, init_db
)
from app.services.strava_service import StravaService, StravaAPIError

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class StravaBackup:
    """Backup all Strava data to SQLite."""

    def __init__(self):
        self.strava = StravaService()
        self.stats = {
            "activities_saved": 0,
            "activities_skipped": 0,
            "gear_saved": 0,
            "streams_saved": 0,
            "errors": []
        }

    async def run(self):
        """Main backup flow."""
        logger.info("=" * 60)
        logger.info("STRAVA BACKUP TO SQLITE")
        logger.info(f"Database: {settings.database_url}")
        logger.info(f"Started: {datetime.now().isoformat()}")
        logger.info("=" * 60)

        # Initialize database
        await init_db()
        logger.info("Database tables created/verified.")

        # Step 1: Backup gear
        await self.backup_gear()

        # Step 2: Backup all activities
        await self.backup_activities()

        # Step 3: Backup streams for activities with GPS
        await self.backup_streams()

        # Summary
        logger.info("=" * 60)
        logger.info("BACKUP COMPLETE")
        logger.info(f"Activities saved: {self.stats['activities_saved']}")
        logger.info(f"Activities skipped (already exist): {self.stats['activities_skipped']}")
        logger.info(f"Gear saved: {self.stats['gear_saved']}")
        logger.info(f"Streams saved: {self.stats['streams_saved']}")
        if self.stats['errors']:
            logger.warning(f"Errors: {len(self.stats['errors'])}")
            for err in self.stats['errors'][:10]:
                logger.warning(f"  - {err}")
        logger.info("=" * 60)

    async def backup_gear(self):
        """Backup all gear from Strava."""
        logger.info("--- Backing up gear ---")
        try:
            gear_list = await self.strava.get_athlete_gear()
            logger.info(f"Found {len(gear_list)} gear items")

            async with async_session() as session:
                for gear in gear_list:
                    # Check if already exists
                    result = await session.execute(
                        select(GearDB).where(
                            GearDB.source == "strava",
                            GearDB.source_id == gear.id
                        )
                    )
                    existing = result.scalar_one_or_none()

                    if existing:
                        # Update
                        existing.name = gear.name
                        existing.distance = gear.distance or 0.0
                        existing.retired = gear.retired or False
                        existing.primary = gear.primary
                    else:
                        # Insert
                        db_gear = GearDB(
                            source="strava",
                            source_id=gear.id,
                            name=gear.name,
                            distance=gear.distance or 0.0,
                            retired=gear.retired or False,
                            primary=gear.primary,
                        )
                        session.add(db_gear)
                        self.stats["gear_saved"] += 1

                await session.commit()

        except Exception as e:
            logger.error(f"Error backing up gear: {e}")
            self.stats["errors"].append(f"Gear: {e}")

    async def backup_activities(self):
        """Backup all activities from Strava with pagination."""
        logger.info("--- Backing up activities ---")

        page = 1
        per_page = 200
        total_fetched = 0

        while True:
            try:
                logger.info(f"Fetching page {page} (per_page={per_page})...")
                params = {"page": page, "per_page": per_page}
                data = await self.strava._make_request("GET", "/athlete/activities", params=params)

                if not data:
                    logger.info(f"No more activities on page {page}. Done.")
                    break

                total_fetched += len(data)
                logger.info(f"Got {len(data)} activities (total fetched: {total_fetched})")

                async with async_session() as session:
                    for activity_data in data:
                        activity_id = str(activity_data.get("id", ""))

                        # Check if already exists
                        result = await session.execute(
                            select(ActivityDB).where(
                                ActivityDB.source == "strava",
                                ActivityDB.source_id == activity_id
                            )
                        )
                        existing = result.scalar_one_or_none()

                        if existing:
                            self.stats["activities_skipped"] += 1
                            continue

                        # Parse dates
                        start_date = self._parse_date(activity_data.get("start_date", ""))
                        start_date_local = self._parse_date(activity_data.get("start_date_local", ""))

                        # Create DB record
                        db_activity = ActivityDB(
                            source="strava",
                            source_id=activity_id,
                            name=activity_data.get("name", "Unnamed"),
                            sport_type=activity_data.get("sport_type", activity_data.get("type", "Unknown")),
                            activity_type=activity_data.get("type"),
                            distance=activity_data.get("distance", 0.0),
                            moving_time=activity_data.get("moving_time", 0),
                            elapsed_time=activity_data.get("elapsed_time", 0),
                            total_elevation_gain=activity_data.get("total_elevation_gain", 0.0),
                            average_speed=activity_data.get("average_speed", 0.0),
                            max_speed=activity_data.get("max_speed", 0.0),
                            average_heartrate=activity_data.get("average_heartrate"),
                            max_heartrate=activity_data.get("max_heartrate"),
                            average_cadence=activity_data.get("average_cadence"),
                            start_date=start_date,
                            start_date_local=start_date_local,
                            timezone=activity_data.get("timezone", ""),
                            start_lat=self._get_latlng(activity_data, "start_latlng", 0),
                            start_lng=self._get_latlng(activity_data, "start_latlng", 1),
                            end_lat=self._get_latlng(activity_data, "end_latlng", 0),
                            end_lng=self._get_latlng(activity_data, "end_latlng", 1),
                            gear_id=activity_data.get("gear_id"),
                            elev_high=activity_data.get("elev_high"),
                            elev_low=activity_data.get("elev_low"),
                            trainer=activity_data.get("trainer", False),
                            manual=activity_data.get("manual", False),
                            private=activity_data.get("private", False),
                            has_heartrate=activity_data.get("has_heartrate", False),
                            has_gps_data=bool(activity_data.get("start_latlng")),
                            raw_data=json.dumps(activity_data, default=str),
                        )
                        session.add(db_activity)
                        self.stats["activities_saved"] += 1

                    await session.commit()

                if len(data) < per_page:
                    break
                page += 1

                # Rate limit protection
                await asyncio.sleep(1)

            except StravaAPIError as e:
                logger.error(f"Strava API error on page {page}: {e}")
                self.stats["errors"].append(f"Activities page {page}: {e}")
                # Wait and retry once
                await asyncio.sleep(5)
                page += 1
            except Exception as e:
                logger.error(f"Unexpected error on page {page}: {e}")
                self.stats["errors"].append(f"Activities page {page}: {e}")
                page += 1

        # Populate gear names from cache
        await self._update_gear_names()

    async def backup_streams(self):
        """Backup GPS streams for activities that have GPS data."""
        logger.info("--- Backing up activity streams (GPS/HR/cadence) ---")

        async with async_session() as session:
            # Get activities with GPS that don't have streams yet
            result = await session.execute(
                select(ActivityDB).where(
                    ActivityDB.source == "strava",
                    ActivityDB.has_gps_data == True,
                    ActivityDB.gpx_file_path == None,  # No GPX saved yet
                ).order_by(ActivityDB.start_date.desc())
            )
            activities = result.scalars().all()

        logger.info(f"Found {len(activities)} activities needing stream backup")

        for i, activity in enumerate(activities):
            try:
                logger.info(f"  [{i+1}/{len(activities)}] Fetching streams for {activity.name} (ID: {activity.source_id})")

                streams = await self.strava.get_activity_streams(int(activity.source_id))

                if "latlng" not in streams:
                    logger.info(f"    No GPS data available, skipping.")
                    continue

                latlngs = streams["latlng"]["data"]
                times = streams.get("time", {}).get("data", [])
                altitudes = streams.get("altitude", {}).get("data", [])
                heartrates = streams.get("heartrate", {}).get("data", [])
                cadences = streams.get("cadence", {}).get("data", [])

                async with async_session() as session:
                    # Check if streams already exist for this activity
                    result = await session.execute(
                        select(ActivityStreamDB).where(
                            ActivityStreamDB.activity_id == activity.id
                        ).limit(1)
                    )
                    if result.scalar_one_or_none():
                        logger.info(f"    Streams already exist, skipping.")
                        continue

                    # Batch insert stream points
                    batch = []
                    for idx in range(len(latlngs)):
                        lat, lng = latlngs[idx]
                        point = ActivityStreamDB(
                            activity_id=activity.id,
                            point_index=idx,
                            latitude=lat,
                            longitude=lng,
                            altitude=altitudes[idx] if idx < len(altitudes) else None,
                            time_offset=times[idx] if idx < len(times) else None,
                            heartrate=heartrates[idx] if idx < len(heartrates) else None,
                            cadence=cadences[idx] if idx < len(cadences) else None,
                        )
                        batch.append(point)

                        # Flush in batches of 1000
                        if len(batch) >= 1000:
                            session.add_all(batch)
                            batch = []

                    if batch:
                        session.add_all(batch)

                    await session.commit()
                    self.stats["streams_saved"] += 1
                    logger.info(f"    Saved {len(latlngs)} stream points.")

                # Rate limit: Strava allows ~100 requests per 15 min
                await asyncio.sleep(2)

            except StravaAPIError as e:
                logger.warning(f"    Error fetching streams: {e}")
                self.stats["errors"].append(f"Stream {activity.source_id}: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"    Unexpected error: {e}")
                self.stats["errors"].append(f"Stream {activity.source_id}: {e}")

    async def _update_gear_names(self):
        """Update gear names in activities based on gear table."""
        async with async_session() as session:
            gear_result = await session.execute(select(GearDB).where(GearDB.source == "strava"))
            gear_map = {g.source_id: g.name for g in gear_result.scalars().all()}

            if not gear_map:
                return

            # Update activities that have gear_id but no gear_name
            act_result = await session.execute(
                select(ActivityDB).where(
                    ActivityDB.gear_id != None,
                    ActivityDB.gear_name == None
                )
            )
            for activity in act_result.scalars().all():
                if activity.gear_id in gear_map:
                    activity.gear_name = gear_map[activity.gear_id]

            await session.commit()
            logger.info(f"Updated gear names using {len(gear_map)} known gear items")

    def _parse_date(self, date_str: str) -> datetime:
        """Parse ISO date string from Strava."""
        if not date_str:
            return datetime.now()
        try:
            # Strava format: "2024-01-15T08:30:00Z"
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now()

    def _get_latlng(self, data: dict, key: str, index: int) -> float | None:
        """Safely extract lat/lng from array field."""
        val = data.get(key)
        if val and isinstance(val, list) and len(val) > index:
            return val[index]
        return None


async def main():
    """Entry point for backup script."""
    backup = StravaBackup()
    await backup.run()


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(main())
