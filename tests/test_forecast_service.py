"""Tests for GPX checkpoint extraction and trail forecast calculation."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.forecast import (
    CheckpointInput,
    ForecastRequest,
    HistoricalActivitySelection,
)
from app.services.forecast_service import (
    ForecastService,
    ForecastServiceError,
    ParsedRoute,
    RoutePoint,
)

SAMPLE_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <metadata><name>Test Trail</name></metadata>
  <wpt lat="55.005" lon="37.005"><ele>150</ele><name>CP 1</name></wpt>
  <trk><name>Test Trail</name><trkseg>
    <trkpt lat="55.000" lon="37.000"><ele>100</ele></trkpt>
    <trkpt lat="55.005" lon="37.005"><ele>150</ele></trkpt>
    <trkpt lat="55.010" lon="37.010"><ele>120</ele></trkpt>
  </trkseg></trk>
</gpx>"""


class FakeActivityService:
    async def get_activity_by_id(self, activity_id: int):
        return SimpleNamespace(
            id=activity_id,
            source="strava",
            name="Historical race",
            distance=30_000,
            total_elevation_gain=1_500,
            elapsed_time=18_000,
            moving_time=16_200,
            start_date=datetime(2025, 6, 1),
            sport_type="TrailRun",
        )


def test_parse_gpx_extracts_named_waypoints():
    service = ForecastService(FakeActivityService())

    route = service.parse_gpx(SAMPLE_GPX, "test.gpx")

    assert route.name == "Test Trail"
    assert route.distance_km > 1
    # Heights are resampled and filtered; a narrow summit loses up to 2 m here.
    assert route.elevation_gain_m == pytest.approx(50, abs=2)
    assert route.elevation_loss_m == pytest.approx(30, abs=2)
    assert len(route.checkpoints) == 1
    assert route.checkpoints[0].name == "CP 1"
    assert 0 < route.checkpoints[0].distance_km < route.distance_km


def test_forecast_request_requires_a_past_race():
    with pytest.raises(ValueError, match="marked as a race"):
        ForecastRequest(
            route_id="f9f4ef5d-326a-46bd-a6ee-64930c0a79c9",
            activities=[
                HistoricalActivitySelection(
                    activity_id=1, source="strava", is_race=False
                )
            ],
        )


@pytest.mark.asyncio
async def test_calculate_returns_checkpoint_and_finish_ranges(tmp_path):
    service = ForecastService(FakeActivityService())
    service.route_storage = tmp_path
    preview = service.store_route(SAMPLE_GPX, "test.gpx")
    request = ForecastRequest(
        route_id=preview.route_id,
        activities=[
            HistoricalActivitySelection(activity_id=123, source="strava", is_race=True)
        ],
        checkpoints=[CheckpointInput(name="Aid", distance_km=0.5)],
        start_time=datetime(2026, 8, 1, 6, 0),
    )

    result = await service.calculate(request)

    assert result.activities_used == 1
    assert result.races_used == 1
    assert result.checkpoints[0].name == "Aid"
    assert result.checkpoints[-1].name == "Финиш"
    assert result.checkpoints[0].expected_seconds < result.expected_finish_seconds
    assert result.checkpoints[-1].expected_seconds == result.expected_finish_seconds
    assert result.moving_time_seconds < result.expected_finish_seconds
    assert result.checkpoints[-1].expected_at is not None
    assert result.profiles_used == 0
    assert result.confidence == "low"
    assert result.uncertainty_percent >= 20
    assert result.moving_time_seconds + result.stop_time_seconds == (
        result.expected_finish_seconds
    )
    assert all(
        cp.optimistic_seconds < cp.expected_seconds < cp.conservative_seconds
        for cp in result.checkpoints
    )


def test_race_forecast_page_is_available():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/race-forecast")

    assert response.status_code == 200
    assert "Прогноз трейловой гонки" in response.text
    assert 'id="checkpoints-body"' in response.text


def dense_gpx():
    import math

    points = []
    for distance in range(0, 5001, 25):
        longitude = math.degrees(distance / 6_371_000)
        elevation = 100 + min(distance, 5000 - distance) * 0.2
        points.append((0.0, longitude, elevation))
    xml = '<gpx version="1.1" creator="test"><trk><trkseg>'
    xml += "".join(
        f'<trkpt lat="{lat}" lon="{lon}"><ele>{ele}</ele></trkpt>'
        for lat, lon, ele in points
    )
    xml += "</trkseg></trk></gpx>"
    return xml.encode(), points


class ProfileActivityService(FakeActivityService):
    async def get_activity_by_id(self, activity_id, source=None):
        activity = await super().get_activity_by_id(activity_id)
        activity.distance = 5000
        activity.elapsed_time = 3600
        activity.moving_time = 3300
        activity.total_elevation_gain = 500
        return activity

    async def get_activity_profile(self, activity_id, source):
        return dense_gpx()[1]


def forecast_request(route_id, **kwargs):
    return ForecastRequest(
        route_id=route_id,
        activities=[
            HistoricalActivitySelection(activity_id=123, source="strava", is_race=True)
        ],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_same_route_reproduces_historical_time_without_calibration_bias(tmp_path):
    service = ForecastService(ProfileActivityService())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    result = await service.calculate(forecast_request(preview.route_id))
    assert result.profiles_used == 1
    assert not result.warnings
    assert result.expected_finish_seconds == 3600
    assert result.moving_time_seconds == 3300
    assert result.stop_time_seconds == 300


@pytest.mark.asyncio
async def test_unavailable_profile_falls_back_without_failing(tmp_path):
    from app.services.unified_service import UnifiedServiceError

    class Unavailable(ProfileActivityService):
        async def get_activity_profile(self, activity_id, source):
            raise UnifiedServiceError("profile unavailable")

    service = ForecastService(Unavailable())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    result = await service.calculate(forecast_request(preview.route_id))
    assert result.profiles_used == 0
    assert result.warnings
    assert result.uncertainty_percent >= 20


@pytest.mark.asyncio
async def test_incomplete_historical_track_does_not_calibrate_pace(tmp_path):
    class Incomplete(ProfileActivityService):
        async def get_activity_profile(self, activity_id, source):
            return dense_gpx()[1][:50]

    service = ForecastService(Incomplete())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    result = await service.calculate(forecast_request(preview.route_id))
    assert result.profiles_used == 0


@pytest.mark.asyncio
async def test_invalid_race_does_not_count_as_calibration_race(tmp_path):
    class InvalidRace(ProfileActivityService):
        async def get_activity_by_id(self, activity_id, source=None):
            activity = await super().get_activity_by_id(activity_id, source)
            if activity_id == 123:
                activity.moving_time = 0
            return activity

    service = ForecastService(InvalidRace())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    request = forecast_request(preview.route_id)
    request.activities.append(
        HistoricalActivitySelection(activity_id=456, source="strava", is_race=False)
    )
    with pytest.raises(ForecastServiceError, match="race must contain valid"):
        await service.calculate(request)


@pytest.mark.asyncio
async def test_duplicate_history_cannot_inflate_confidence(tmp_path):
    service = ForecastService(ProfileActivityService())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    request = forecast_request(preview.route_id)
    request.activities *= 5
    with pytest.raises(ForecastServiceError, match="only once"):
        await service.calculate(request)


def test_checkpoint_interpolates_time_and_elevation_between_track_points():
    route = ParsedRoute(
        "sparse",
        [
            RoutePoint(0, 0, 100),
            RoutePoint(0, 0.01, 200, distance_km=1, weighted_effort=5),
        ],
        [],
    )
    fraction, elevation = ForecastService._checkpoint_fraction(route, 0.25)
    assert fraction == pytest.approx(0.25)
    assert elevation == pytest.approx(125)


@pytest.mark.asyncio
async def test_rounded_finish_checkpoint_still_matches_finish_time(tmp_path):
    service = ForecastService(ProfileActivityService())
    service.route_storage = tmp_path
    preview = service.store_route(dense_gpx()[0], "race.gpx")
    request = forecast_request(
        preview.route_id,
        checkpoints=[CheckpointInput(name="Finish", distance_km=4.995)],
    )
    result = await service.calculate(request)
    assert len(result.checkpoints) == 1
    assert result.checkpoints[-1].expected_seconds == result.expected_finish_seconds


def test_disconnected_gpx_segments_are_not_joined_into_an_invented_climb():
    xml = SAMPLE_GPX.replace(
        b'<trkpt lat="55.005"', b'</trkseg><trkseg><trkpt lat="55.005"'
    )
    with pytest.raises(ForecastServiceError, match="disconnected"):
        ForecastService(FakeActivityService()).parse_gpx(xml)
