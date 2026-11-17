"""Garmin activities via the official Garmin Connect Personal API."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import SecretStr

from app.config import settings
from app.models.strava import Activity, Gear  # Reuse existing Pydantic models
from app.services.garmin_api import GarminAPIError, GarminPersonalClient
from app.services.gear_policy import requires_gear
from app.utils.garmin_fit import activity_file_to_gpx

logger = logging.getLogger(__name__)


class GarminService:
    """Activity model adapter for the read-only Personal API."""

    supports_gear = False

    def __init__(self):
        self.client: Optional[GarminPersonalClient] = None

    @staticmethod
    def is_configured() -> bool:
        return bool(
            settings.garmin_api_key_file
            or settings.garmin_api_key.get_secret_value().strip()
        )

    async def _ensure_connected(self) -> None:
        """Load the API key without using Garmin account credentials."""
        if self.client is not None:
            return

        key = settings.garmin_api_key.get_secret_value().strip()
        if settings.garmin_api_key_file:
            try:
                key = (
                    await asyncio.to_thread(
                        Path(settings.garmin_api_key_file).expanduser().read_text,
                        encoding="utf-8",
                    )
                ).strip()
            except (OSError, UnicodeError):
                raise GarminAPIError("Unable to read GARMIN_API_KEY_FILE") from None
        if not key:
            raise GarminAPIError("Configure GARMIN_API_KEY or GARMIN_API_KEY_FILE")
        self.client = GarminPersonalClient(SecretStr(key))

    async def get_health(self, metric: str, day: str) -> Any:
        await self._ensure_connected()
        return await self.client.get_health(metric, day)

    def _garmin_activity_to_model(self, data: Dict[str, Any]) -> Activity:
        """Convert Garmin activity dict to our Activity Pydantic model."""
        # Activity-list and activity-detail endpoints use different nesting.
        summary = data.get("summaryDTO", data)
        activity_id = data.get("activityId", 0)
        start_time_str = summary.get("startTimeLocal", "") or summary.get(
            "startTimeGMT", ""
        )
        start_time_gmt = summary.get("startTimeGMT", "") or start_time_str

        # Parse datetime
        start_local = self._parse_garmin_datetime(start_time_str)
        start_utc = self._parse_garmin_datetime(start_time_gmt)

        # Map Garmin activity type to a unified type
        activity_type = data.get("activityType") or data.get("activityTypeDTO", {})
        type_key = (
            activity_type.get("typeKey", "other")
            if isinstance(activity_type, dict)
            else "other"
        )
        sport_type = self._map_garmin_sport_type(type_key)

        # Personal API has no gear endpoints or authoritative gear fields.
        metadata = data.get("metadataDTO") or {}
        privacy = (data.get("accessControlRuleDTO") or {}).get("typeKey", "public")
        activity_timezone = data.get("timeZoneUnitDTO") or {}

        start_lat = summary.get("startLatitude")
        start_lng = summary.get("startLongitude")
        start_latlng = (
            [start_lat, start_lng]
            if start_lat is not None and start_lng is not None
            else None
        )

        return Activity(
            source="garmin",
            resource_state=2,
            athlete=None,
            name=data.get("activityName", "Unnamed Activity"),
            distance=summary.get("distance", 0.0) or 0.0,
            moving_time=int(
                summary.get("movingDuration", 0) or summary.get("duration", 0) or 0
            ),
            elapsed_time=int(
                summary.get("elapsedDuration") or summary.get("duration") or 0
            ),
            total_elevation_gain=summary.get("elevationGain", 0.0) or 0.0,
            type=sport_type,
            sport_type=sport_type,
            id=activity_id,
            start_date=start_utc,
            start_date_local=start_local,
            timezone=activity_timezone.get("timeZone")
            or activity_timezone.get("unitKey")
            or "UTC",
            utc_offset=(start_local - start_utc).total_seconds(),
            achievement_count=0,
            kudos_count=0,
            comment_count=0,
            athlete_count=1,
            photo_count=0,
            trainer=summary.get("isIndoor", False) or False,
            commute=False,
            manual=metadata.get(
                "manualActivity", metadata.get("isManualActivity", False)
            )
            or False,
            private=metadata.get("isPrivate", privacy == "private") or False,
            visibility={
                "public": "everyone",
                "private": "only_me",
                "subscribers": "followers_only",
            }.get(privacy, privacy),
            flagged=False,
            gear_id=None,
            gear_name=None,
            gear_status_known=False,
            start_latlng=start_latlng,
            average_speed=summary.get("averageSpeed", 0.0) or 0.0,
            max_speed=summary.get("maxSpeed", 0.0) or 0.0,
            average_cadence=summary.get("averageRunningCadenceInStepsPerMinute")
            or summary.get("averageCadence")
            or summary.get("averageRunCadence")
            or summary.get("averageBikeCadence")
            or summary.get("averageSwimCadence"),
            has_heartrate=bool(summary.get("averageHR")),
            average_heartrate=summary.get("averageHR"),
            max_heartrate=summary.get("maxHR"),
            elev_high=summary.get("maxElevation", summary.get("elevationMax")),
            elev_low=summary.get("minElevation", summary.get("elevationMin")),
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
        *,
        include_gear: bool = True,
    ) -> List[Activity]:
        """Get activities from Garmin Connect."""
        await self._ensure_connected()

        try:
            if after or before:
                # Use date-based search
                start_date = after.strftime("%Y-%m-%d") if after else "2000-01-01"
                end_date = (
                    before.strftime("%Y-%m-%d")
                    if before
                    else datetime.now().strftime("%Y-%m-%d")
                )
                raw_activities = await self.client.get_activities_by_date(
                    start_date, end_date
                )
            else:
                raw_activities = await self.client.get_activities(start, limit)

            activities = [self._garmin_activity_to_model(a) for a in raw_activities]

            # Sort newest first
            activities.sort(key=lambda x: x.start_date, reverse=True)
            return activities

        except GarminAPIError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise GarminAPIError("Garmin returned invalid activity data") from None

    async def get_activity_by_id(self, activity_id: int) -> Activity:
        """Get a single activity by ID."""
        await self._ensure_connected()

        try:
            data = await self.client.get_activity(activity_id)
            activity = self._garmin_activity_to_model(data)
            return activity
        except GarminAPIError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise GarminAPIError("Garmin returned invalid activity data") from None

    async def get_activities_without_gear(
        self, after: Optional[datetime] = None
    ) -> List[Activity]:
        """Get activities without gear assigned."""
        activities = await self.get_activities(
            after=after, before=datetime.now(), limit=200
        )
        return [
            a
            for a in activities
            if a.gear_status_known and not a.gear_id and requires_gear(a)
        ]

    async def get_athlete_gear(self) -> List[Gear]:
        """Personal API does not expose gear; archived gear remains in SQLite."""
        return []

    async def download_gpx(
        self,
        activity_id: int,
        save_path: Optional[str] = None,
        activity_name: Optional[str] = None,
    ) -> str:
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

        safe_name = activity_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        filename = f"garmin_{activity_id}_{safe_name}.gpx"
        file_path = os.path.join(save_path, filename)

        # Check cache
        if os.path.exists(file_path):
            return file_path

        activity_file = await self.client.download_activity(activity_id)
        gpx_data = await asyncio.to_thread(
            activity_file_to_gpx, activity_file, activity_name
        )
        await asyncio.to_thread(Path(file_path).write_text, gpx_data, encoding="utf-8")
        logger.info("Downloaded GPX for Garmin activity %s", activity_id)
        return file_path
