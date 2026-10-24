from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.models.strava import ActivityFilter
from app.services import remote_activity_service
from app.services.garmin_service import GarminService
from app.services.remote_activity_service import RemoteActivityService
from app.services.unified_service import UnifiedServiceError


def strava_activity():
    activity = GarminService()._garmin_activity_to_model(
        {
            "activityId": 456,
            "activityName": "Secondary run",
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-10-23 12:00:00",
            "startTimeGMT": "2026-10-23 09:00:00",
        }
    )
    return activity.model_copy(update={"source": "strava"}).model_dump(mode="json")


def use_transport(monkeypatch, handler):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        remote_activity_service.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_missing_gear_uses_secondary_api_and_date_filter(monkeypatch):
    def respond(request):
        assert request.url.host == "secondary"
        assert request.url.path == "/api/v1/activities/no-gear"
        assert request.url.params["after"] == "2026-10-22T18:00:00"
        return httpx.Response(200, json=[strava_activity()])

    use_transport(monkeypatch, respond)
    service = RemoteActivityService("http://secondary:8000/")
    activities = await service.get_activities_without_gear(datetime(2026, 10, 22, 18))
    assert [(item.id, item.source) for item in activities] == [(456, "strava")]


@pytest.mark.asyncio
async def test_gpx_is_downloaded_from_secondary_and_cached_separately(
    monkeypatch, tmp_path
):
    def respond(request):
        if request.url.path == "/api/v1/activities/456":
            return httpx.Response(200, json=strava_activity())
        assert request.url.path == "/api/v1/activities/456/gpx"
        return httpx.Response(200, content=b"<gpx/>")

    use_transport(monkeypatch, respond)
    service = RemoteActivityService("http://secondary:8000")
    assert (await service.get_activity_by_id(456)).source == "strava"
    path = await service.download_gpx(456, str(tmp_path), activity_name="Run")
    assert Path(path).name == "secondary_456.gpx"
    assert Path(path).read_bytes() == b"<gpx/>"


@pytest.mark.asyncio
async def test_secondary_api_failure_is_reported_without_using_primary(monkeypatch):
    use_transport(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(UnifiedServiceError, match="Secondary account API"):
        await RemoteActivityService(
            "http://secondary:8000"
        ).get_activities_without_gear()


@pytest.mark.asyncio
async def test_secondary_rejects_a_garmin_link_without_fetching_it(monkeypatch):
    def unexpected(request):
        raise AssertionError("Strava account must not fetch a Garmin activity")

    use_transport(monkeypatch, unexpected)
    with pytest.raises(UnifiedServiceError, match="Strava activities"):
        await RemoteActivityService("http://secondary:8000").get_activity_by_id(
            456, source="garmin"
        )


@pytest.mark.asyncio
async def test_secondary_reads_all_pages_for_today_without_filtering_gear(monkeypatch):
    pages = []

    def respond(request):
        assert request.url.path == "/api/v1/activities"
        assert request.url.params["after"] == "2026-10-22T20:59:59Z"
        assert request.url.params["before"] == "2026-10-23T21:00:00Z"
        assert "has_gear" not in request.url.params
        assert "gear_id" not in request.url.params
        page = int(request.url.params["page"])
        pages.append(page)
        activity = strava_activity()
        activity.update(id=455 + page, gear_id=None if page == 1 else "new-shoes")
        return httpx.Response(200, json={"items": [activity], "total_pages": 2})

    use_transport(monkeypatch, respond)
    activities = await RemoteActivityService("http://secondary:8000").get_activities(
        ActivityFilter(
            after=datetime(2026, 10, 22, 20, 59, 59, tzinfo=timezone.utc),
            before=datetime(2026, 10, 23, 21, tzinfo=timezone.utc),
        ),
        all_pages=True,
        include_gear=False,
    )

    assert pages == [1, 2]
    assert [(item.id, item.gear_id) for item in activities] == [
        (456, None),
        (457, "new-shoes"),
    ]
