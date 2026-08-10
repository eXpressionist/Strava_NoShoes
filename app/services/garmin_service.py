"""Garmin Connect service using the community python-garminconnect library."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:  # Strava remains usable before cutover in minimal installs.
    Garmin = None

    class GarminConnectAuthenticationError(Exception):
        pass

from app.config import settings
from app.models.strava import Activity, Gear  # Reuse existing Pydantic models

logger = logging.getLogger(__name__)


class GarminAPIError(Exception):
    """Custom exception for Garmin API errors."""
    pass


class GarminService:
    """Service class for interacting with Garmin Connect via python-garminconnect."""

    def __init__(self):
        self.email = settings.garmin_email
        self.password = settings.garmin_password
        self.token_store = settings.garmin_token_store
        self.client: Optional[Garmin] = None
        self._gear_cache: Dict[str, str] = {}

    async def _ensure_connected(self) -> None:
        """Ensure we have an authenticated Garmin session."""
        if self.client is not None:
            return

        if not self.email or not self.password:
            raise GarminAPIError("Garmin credentials not configured. Set GARMIN_EMAIL and GARMIN_PASSWORD.")
        if Garmin is None:
            raise GarminAPIError("garminconnect dependency is not installed")

        try:
            self.client = Garmin(self.email, self.password)

            token_dir = Path(self.token_store)
            token_dir.mkdir(parents=True, exist_ok=True)
            # Current garminconnect versions load, refresh and persist tokens
            # when the token-store path is passed to login.
            await asyncio.to_thread(self.client.login, str(token_dir))
            logger.info("Garmin: authenticated using persistent session tokens")

        except GarminConnectAuthenticationError as e:
            self.client = None
            raise GarminAPIError(f"Garmin authentication failed: {e}")
        except Exception as e:
            self.client = None
            raise GarminAPIError(f"Garmin connection error: {e}")

    def _garmin_activity_to_model(self, data: Dict[str, Any]) -> Activity:
        """Convert Garmin activity dict to our Activity Pydantic model."""
        # Activity-list and activity-detail endpoints use different nesting.
        summary = data.get("summaryDTO", data)
        activity_id = data.get("activityId", 0)
        start_time_str = summary.get("startTimeLocal", "") or summary.get("startTimeGMT", "")
        start_time_gmt = summary.get("startTimeGMT", "") or start_time_str

        # Parse datetime
        start_local = self._parse_garmin_datetime(start_time_str)
        start_utc = self._parse_garmin_datetime(start_time_gmt)

        # Map Garmin activity type to a unified type
        activity_type = data.get("activityType") or data.get("activityTypeDTO", {})
        type_key = activity_type.get("typeKey", "other") if isinstance(activity_type, dict) else "other"
        sport_type = self._map_garmin_sport_type(type_key)

        # Gear
        gear_name = None
        gear_id = None
        # Garmin may include gear info in metadataDTO or separate field
        metadata = data.get("metadataDTO", {})
        if isinstance(metadata, dict):
            gear_list = metadata.get("associatedGearIds", [])
            if gear_list:
                gear_id = str(gear_list[0]) if gear_list else None

        return Activity(
            source="garmin",
            resource_state=2,
            athlete=None,
            name=data.get("activityName", "Unnamed Activity"),
            distance=summary.get("distance", 0.0) or 0.0,
            moving_time=int(summary.get("movingDuration", 0) or summary.get("duration", 0) or 0),
            elapsed_time=int(summary.get("duration", 0) or 0),
            total_elevation_gain=summary.get("elevationGain", 0.0) or 0.0,
            type=sport_type,
            sport_type=sport_type,
            id=activity_id,
            start_date=start_utc,
            start_date_local=start_local,
            timezone="(GMT+00:00) UTC",
            utc_offset=0.0,
            achievement_count=0,
            kudos_count=0,
            comment_count=0,
            athlete_count=1,
            photo_count=0,
            trainer=summary.get("isIndoor", False) or False,
            commute=False,
            manual=metadata.get("isManualActivity", False) or False,
            private=metadata.get("isPrivate", False) or False,
            visibility="everyone",
            flagged=False,
            gear_id=gear_id,
            gear_name=gear_name,
            average_speed=summary.get("averageSpeed", 0.0) or 0.0,
            max_speed=summary.get("maxSpeed", 0.0) or 0.0,
            average_cadence=summary.get("averageRunningCadenceInStepsPerMinute") or summary.get("averageCadence"),
            has_heartrate=bool(summary.get("averageHR")),
            average_heartrate=summary.get("averageHR"),
            max_heartrate=summary.get("maxHR"),
            elev_high=summary.get("elevationMax"),
            elev_low=summary.get("elevationMin"),
            pr_count=0,
            total_photo_count=0,
            has_kudoed=False,
        )

    def _parse_garmin_datetime(self, dt_str: str) -> datetime:
        """Parse Garmin datetime string."""
        if not dt_str:
            return datetime.now()
        # Garmin format: "2024-01-15 08:30:00" or "2024-01-15T08:30:00.000"
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
            try:
                return datetime.strptime(dt_str, fmt)
            except ValueError:
                continue
        # Fallback
        return datetime.now()

    def _map_garmin_sport_type(self, type_key: str) -> str:
        """Map Garmin activity typeKey to unified sport type."""
        mapping = {
            "running": "Run",
            "trail_running": "TrailRun",
            "treadmill_running": "Run",
            "cycling": "Ride",
            "mountain_biking": "MountainBikeRide",
            "indoor_cycling": "VirtualRide",
            "walking": "Walk",
            "hiking": "Hike",
            "swimming": "Swim",
            "pool_swimming": "Swim",
            "open_water_swimming": "Swim",
            "strength_training": "WeightTraining",
            "yoga": "Yoga",
            "elliptical": "Elliptical",
            "other": "Workout",
        }
        return mapping.get(type_key, "Workout")

    async def get_activities(
        self,
        start: int = 0,
        limit: int = 20,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
    ) -> List[Activity]:
        """Get activities from Garmin Connect."""
        await self._ensure_connected()

        try:
            if after or before:
                # Use date-based search
                start_date = after.strftime("%Y-%m-%d") if after else "2000-01-01"
                end_date = before.strftime("%Y-%m-%d") if before else datetime.now().strftime("%Y-%m-%d")
                raw_activities = await asyncio.to_thread(
                    self.client.get_activities_by_date, start_date, end_date
                )
            else:
                raw_activities = await asyncio.to_thread(
                    self.client.get_activities, start, limit
                )

            activities = [self._garmin_activity_to_model(a) for a in raw_activities]

            # Populate gear names
            await self._populate_gear_names(activities)

            # Sort newest first
            activities.sort(key=lambda x: x.start_date, reverse=True)
            return activities

        except GarminConnectAuthenticationError:
            # Session expired, try re-auth
            self.client = None
            await self._ensure_connected()
            return await self.get_activities(start, limit, after, before)
        except Exception as e:
            raise GarminAPIError(f"Failed to fetch activities: {e}")

    async def get_activity_by_id(self, activity_id: int) -> Activity:
        """Get a single activity by ID."""
        await self._ensure_connected()

        try:
            data = await asyncio.to_thread(self.client.get_activity, activity_id)
            activity = self._garmin_activity_to_model(data)
            await self._populate_gear_names([activity])
            return activity
        except Exception as e:
            raise GarminAPIError(f"Failed to fetch activity {activity_id}: {e}")

    async def get_activities_without_gear(self, after: Optional[datetime] = None) -> List[Activity]:
        """Get activities without gear assigned."""
        activities = await self.get_activities(
            after=after,
            before=datetime.now(),
            limit=200
        )
        return [a for a in activities if not a.gear_id]

    async def get_athlete_gear(self) -> List[Gear]:
        """Return gear observed on recent Garmin activities."""
        await self._ensure_connected()

        try:
            raw_activities = await asyncio.to_thread(self.client.get_activities, 0, 100)
            gear_by_id: Dict[str, Gear] = {}
            for raw in raw_activities:
                activity_id = raw.get("activityId")
                if not activity_id:
                    continue
                payload = await asyncio.to_thread(
                    self.client.get_activity_gear, activity_id
                )
                for item in self._gear_items(payload):
                    gear = self._gear_model(item)
                    if gear:
                        gear_by_id[gear.id] = gear
                        self._gear_cache[gear.id] = gear.name
            return list(gear_by_id.values())
        except Exception as e:
            logger.warning(f"Failed to fetch Garmin gear: {e}")
            return []

    async def _populate_gear_names(self, activities: List[Activity]) -> None:
        """Use Garmin's activity-specific gear endpoint.

        Activity summaries do not reliably include gear. Treating that field as
        authoritative would therefore notify for every Garmin activity.
        """
        for activity in activities:
            if activity.gear_id:
                activity.gear_name = self._gear_cache.get(activity.gear_id)
                continue
            payload = await asyncio.to_thread(
                self.client.get_activity_gear, activity.id
            )
            items = self._gear_items(payload)
            if not items:
                continue
            gear = self._gear_model(items[0])
            if gear:
                activity.gear_id = gear.id
                activity.gear_name = gear.name
                self._gear_cache[gear.id] = gear.name

    @staticmethod
    def _gear_items(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("gearDTOs", "activityGear", "gear"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
            if any(key in payload for key in ("uuid", "gearUUID", "gearPk")):
                return [payload]
        return []

    @staticmethod
    def _gear_model(item: Dict[str, Any]) -> Optional[Gear]:
        gear_id = item.get("uuid") or item.get("gearUUID") or item.get("gearPk")
        if gear_id is None:
            return None
        name = (
            item.get("customMakeModel")
            or item.get("displayName")
            or item.get("gearName")
            or "Unknown"
        )
        return Gear(
            id=str(gear_id),
            primary=bool(item.get("isDefault", False)),
            name=str(name),
            resource_state=2,
            retired=not item.get("isActive", True),
            distance=item.get("totalDistance", 0.0),
        )

    async def download_gpx(self, activity_id: int, save_path: Optional[str] = None, activity_name: Optional[str] = None) -> str:
        """Download GPX file for an activity from Garmin."""
        await self._ensure_connected()

        if not save_path:
            save_path = settings.gpx_storage_path

        Path(save_path).mkdir(parents=True, exist_ok=True)

        # Determine filename
        if not activity_name:
            try:
                activity = await self.get_activity_by_id(activity_id)
                activity_name = activity.name
            except Exception:
                activity_name = f"activity_{activity_id}"

        safe_name = activity_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        filename = f"{safe_name}.gpx"
        file_path = os.path.join(save_path, filename)

        # Check cache
        if os.path.exists(file_path):
            return file_path

        try:
            # Garmin provides direct GPX download
            gpx_data = await asyncio.to_thread(
                self.client.download_activity,
                activity_id,
                self.client.ActivityDownloadFormat.GPX,
            )

            with open(file_path, "wb") as f:
                f.write(gpx_data)

            logger.info(f"Downloaded GPX for activity {activity_id} -> {file_path}")
            return file_path

        except Exception as e:
            raise GarminAPIError(f"Failed to download GPX for activity {activity_id}: {e}")
