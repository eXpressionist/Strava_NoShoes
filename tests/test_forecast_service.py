"""Tests for GPX checkpoint extraction and trail forecast calculation."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.forecast import (
    CheckpointInput,
    ForecastRequest,
    HistoricalActivitySelection,
)
from app.services.forecast_service import ForecastService


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
    assert route.elevation_gain_m == 50
    assert route.elevation_loss_m == 30
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
            HistoricalActivitySelection(
                activity_id=123, source="strava", is_race=True
            )
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


def test_race_forecast_page_is_available():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/race-forecast")

    assert response.status_code == 200
    assert "Прогноз трейловой гонки" in response.text
    assert 'id="checkpoints-body"' in response.text
