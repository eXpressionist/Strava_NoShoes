"""Read the secondary Strava instance without sharing its credentials or database."""

from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.models.strava import Activity, ActivityFilter
from app.services.unified_service import UnifiedServiceError


class RemoteActivityService:
    """Activity operations used by the shared bot for the Strava-only account."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def strava_is_live(self) -> bool:
        return True

    async def _get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()
                return response
        except httpx.HTTPError as exc:
            raise UnifiedServiceError("Secondary account API is unavailable") from exc

    async def get_activities_without_gear(
        self, after: Optional[datetime] = None
    ) -> list[Activity]:
        response = await self._get(
            "/api/v1/activities/no-gear",
            {"after": after.isoformat()} if after else None,
        )
        return [Activity.model_validate(item) for item in response.json()]

    async def get_activities(
        self,
        activity_filter: Optional[ActivityFilter] = None,
        all_pages: bool = False,
        *,
        include_gear: bool = True,
    ) -> list[Activity]:
        params = (activity_filter or ActivityFilter()).model_dump(
            mode="json", exclude_none=True
        )
        if all_pages:
            params.update(page=1, per_page=200)
        activities = []
        while True:
            response = await self._get("/api/v1/activities", params)
            data = response.json()
            items = data["items"]
            activities.extend(Activity.model_validate(item) for item in items)
            if not all_pages or not items or params["page"] >= data["total_pages"]:
                return activities
            params["page"] += 1

    async def get_activity_by_id(
        self, activity_id: int, source: Optional[str] = None
    ) -> Activity:
        if source not in (None, "strava"):
            raise UnifiedServiceError("This account uses Strava activities")
        response = await self._get(f"/api/v1/activities/{activity_id}")
        return Activity.model_validate(response.json())

    async def download_gpx(
        self,
        activity_id: int,
        save_path: Optional[str] = None,
        activity_name: Optional[str] = None,
    ) -> str:
        response = await self._get(
            f"/api/v1/activities/{activity_id}/gpx",
            {"activity_name": activity_name} if activity_name else None,
        )
        directory = Path(save_path or settings.gpx_storage_path)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"secondary_{activity_id}.gpx"
        path.write_bytes(response.content)
        return str(path)
