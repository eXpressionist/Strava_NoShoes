"""Official API authentication, pagination, and credential handling."""

import asyncio
import base64
import json
import time
from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.models.strava import ActivityFilter
from app.services.garmin_api import TOKEN_PATH, GarminAPIError, GarminPersonalClient
from app.services.garmin_service import GarminService
from app.services.gear_migration_service import GearMigration
from app.services.unified_service import UnifiedActivityService
from tests.test_garmin_service import garmin_activity


def bearer(expiry=None):
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"exp": expiry or time.time() + 86400}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"header.{payload}.signature"


def client_with(handler):
    return GarminPersonalClient(
        SecretStr("private-api-key"), transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_key_exchange_and_cached_bearer_with_concurrent_requests():
    requests = []
    token = bearer()

    async def handler(request):
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            await asyncio.sleep(0)
            assert request.headers["authorization"] == "ApiKey private-api-key"
            assert request.headers["accept"] == "*/*"
            return httpx.Response(200, text=token)
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.url.host == "personal.connectapi.garmin.com"
        assert "private-api-key" not in str(request.url)
        return httpx.Response(200, json=[])

    client = client_with(handler)
    assert await asyncio.gather(*(client.get_activities() for _ in range(3))) == [
        [],
        [],
        [],
    ]
    assert sum(r.method == "POST" for r in requests) == 1
    client._expires_at = time.time() - 1
    await client.get_activities()
    assert sum(r.method == "POST" for r in requests) == 2


@pytest.mark.asyncio
async def test_unauthorized_request_renews_token_only_once():
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(200, text=bearer())
            if request.method == "POST"
            else httpx.Response(401, text="private response")
        )

    with pytest.raises(GarminAPIError) as error:
        await client_with(handler).get_activities()
    assert error.value.status_code == 401
    assert [r.method for r in requests] == ["POST", "GET", "POST", "GET"]
    assert "private" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500])
async def test_errors_do_not_leak_response_bodies_or_retry(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="private-api-key and health data")

    with pytest.raises(GarminAPIError) as error:
        await client_with(handler).get_activities()
    assert error.value.status_code == status
    assert "private-api-key" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_date_search_fetches_every_page():
    offsets = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text=bearer())
        assert request.url.params["startDate"] == "2026-10-16"
        assert request.url.params["endDate"] == "2026-10-17"
        start = int(request.url.params["start"])
        offsets.append(start)
        return httpx.Response(
            200, json=[{"activityId": i} for i in range(start, min(start + 100, 205))]
        )

    activities = await client_with(handler).get_activities_by_date(
        "2026-10-16", "2026-10-17"
    )
    assert len(activities) == 205
    assert offsets == [0, 100, 200]


@pytest.mark.asyncio
async def test_repeated_activity_page_stops():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text=bearer())
        return httpx.Response(200, json=[{"activityId": i} for i in range(100)])

    with pytest.raises(GarminAPIError, match="did not advance"):
        await client_with(handler).get_activities_by_date("2026-10-16", "2026-10-17")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metric,path,params",
    [
        (
            "daily_summary",
            "/usersummary-service/usersummary/daily",
            {"calendarDate": "2026-10-16"},
        ),
        ("sleep", "/sleep-service/sleep/dailySleep", {"date": "2026-10-16"}),
        ("hrv", "/hrv-service/hrv/2026-10-16", {}),
        (
            "body_battery",
            "/wellness-service/wellness/bodyBattery/reports/daily",
            {"startDate": "2026-10-16", "endDate": "2026-10-16"},
        ),
        ("stress", "/usersummary-service/stats/stress/daily/2026-10-16/2026-10-16", {}),
        (
            "heart_rate",
            "/wellness-service/wellness/dailyHeartRate",
            {"date": "2026-10-16"},
        ),
        ("steps", "/usersummary-service/stats/steps/daily/2026-10-16/2026-10-16", {}),
        ("spo2", "/wellness-service/wellness/daily/spo2acclimation/2026-10-16", {}),
        (
            "body_composition",
            "/weight-service/weight/range/2026-10-16/2026-10-16",
            {"includeAll": "true"},
        ),
        (
            "hydration",
            "/usersummary-service/stats/hydration/daily/2026-10-16/2026-10-16",
            {},
        ),
    ],
)
async def test_health_endpoints_use_documented_parameters(metric, path, params):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text=bearer())
        assert request.url.path == path
        assert dict(request.url.params) == params
        return httpx.Response(200, json={"raw": [1, None]})

    assert await client_with(handler).get_health(metric, "2026-10-16") == {
        "raw": [1, None]
    }


@pytest.mark.asyncio
async def test_secret_file_takes_precedence_and_unreadable_file_does_not_fall_back(
    tmp_path, monkeypatch
):
    key_file = tmp_path / "api-key"
    key_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setattr(settings, "garmin_api_key_file", str(key_file))
    monkeypatch.setattr(settings, "garmin_api_key", SecretStr("env-secret"))
    service = GarminService()
    await service._ensure_connected()
    assert service.client._api_key.get_secret_value() == "file-secret"
    key_file.unlink()
    with pytest.raises(GarminAPIError, match="Unable to read GARMIN_API_KEY_FILE"):
        await GarminService()._ensure_connected()


def test_settings_and_client_repr_hide_api_key():
    config = Settings(_env_file=None, garmin_api_key="private-api-key")
    assert "private-api-key" not in repr(config)
    assert "private-api-key" not in repr(client_with(lambda request: None))
    assert "garmin_password" not in Settings.model_fields
    assert "garmin_email" not in Settings.model_fields


@pytest.mark.asyncio
async def test_gear_migration_fails_before_reading_or_writing(monkeypatch):
    migration = GearMigration()
    connect = AsyncMock()
    monkeypatch.setattr(migration.garmin, "_ensure_connected", connect)
    with pytest.raises(GarminAPIError, match="does not support"):
        await migration.build_plan(None, date.today())
    with pytest.raises(GarminAPIError, match="does not support"):
        await migration.apply({}, lambda event: None)
    connect.assert_not_called()


def test_unknown_gear_is_excluded_from_missing_gear_filter():
    activity = GarminService()._garmin_activity_to_model(garmin_activity())
    service = UnifiedActivityService()
    assert service._apply_filters([activity], ActivityFilter(has_gear=False)) == []
    assert service._apply_filters([activity], ActivityFilter()) == [activity]
    activity.gear_id = "archived-shoe"
    assert service._apply_filters([activity], ActivityFilter(has_gear=True)) == []
    assert (
        service._apply_filters([activity], ActivityFilter(gear_id="archived-shoe"))
        == []
    )
