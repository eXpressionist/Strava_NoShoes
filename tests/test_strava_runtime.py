"""Ensure runtime source selection and Strava token sharing are stable."""

import asyncio
import json
import time
from datetime import date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.forecast_routes import service as forecast_api_service
from app.api.routes import athlete_service, service as api_service
from app.config import settings
from app.main import bot_service
from app.models.database import ActivityDB, Base
from app.models.strava import ActivityFilter
from app.services import unified_service
from app.services.garmin_service import GarminAPIError
from app.services.strava_service import StravaAPIError, StravaService
from app.services.unified_service import UnifiedActivityService


def test_runtime_services_use_scheduled_cutover():
    assert isinstance(api_service, UnifiedActivityService)
    assert isinstance(forecast_api_service.activities, UnifiedActivityService)
    assert isinstance(bot_service.activity_service, UnifiedActivityService)
    assert isinstance(athlete_service, StravaService)


def test_cutoff_day_is_inclusive(monkeypatch):
    monkeypatch.setattr(settings, "migration_cutoff", "2026-10-15")
    service = UnifiedActivityService()

    assert service.strava_is_live(date(2026, 10, 15)) is True
    assert service.strava_is_live(date(2026, 10, 16)) is False
    assert service._cutoff.isoformat() == "2026-10-16T00:00:00"


@pytest.mark.asyncio
async def test_post_cutoff_garmin_failure_uses_sqlite_sync(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        session.add(
            ActivityDB(
                source="garmin",
                source_id="901",
                name="Synchronized Run",
                sport_type="Run",
                activity_type="Run",
                start_date=datetime(2026, 10, 17, 4, 0),
                start_date_local=datetime(2026, 10, 17, 8, 0),
            )
        )
        await session.commit()

    monkeypatch.setattr(settings, "migration_cutoff", "2026-10-15")
    monkeypatch.setattr(unified_service, "async_session", session_factory)
    service = UnifiedActivityService()
    service._db_initialized = True
    monkeypatch.setattr(service, "strava_is_live", lambda today=None: False)

    async def unavailable(*args, **kwargs):
        raise GarminAPIError("temporary outage")

    monkeypatch.setattr(service.garmin, "get_activities", unavailable)

    activities = await service.get_activities(
        ActivityFilter(after=datetime(2026, 10, 16))
    )

    assert [activity.id for activity in activities] == [901]
    assert activities[0].source == "garmin"
    await engine.dispose()


def test_tokens_for_another_strava_client_are_ignored(tmp_path, monkeypatch):
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "client_id": "primary-client",
                "access_token": "primary-access",
                "refresh_token": "primary-refresh",
                "expires_at": 2_000_000_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "strava_client_id", "secondary-client")
    monkeypatch.setattr(settings, "strava_access_token", "secondary-access")
    monkeypatch.setattr(settings, "strava_refresh_token", "secondary-refresh")
    monkeypatch.setattr(settings, "strava_token_expires_at", 1_900_000_000)
    monkeypatch.setattr(settings, "strava_token_file", str(token_file))

    service = StravaService()

    assert service.access_token == "secondary-access"
    assert service.refresh_token == "secondary-refresh"


@pytest.mark.asyncio
async def test_second_401_is_reported_as_strava_auth_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "strava_client_id", "secondary-client")
    monkeypatch.setattr(settings, "strava_client_secret", "secondary-secret")
    monkeypatch.setattr(settings, "strava_access_token", "secondary-access")
    monkeypatch.setattr(settings, "strava_refresh_token", "secondary-refresh")
    monkeypatch.setattr(settings, "strava_token_expires_at", 2_000_000_000)
    monkeypatch.setattr(settings, "strava_token_file", str(tmp_path / "tokens.json"))

    unauthorized = httpx.Response(
        401,
        json={"message": "Authorization Error"},
        request=httpx.Request("GET", "https://www.strava.com/api/v3/athlete"),
    )
    refreshed = httpx.Response(
        200,
        json={
            "access_token": "refreshed-access",
            "refresh_token": "refreshed-refresh",
            "expires_at": 2_000_000_100,
        },
        request=httpx.Request("POST", "https://www.strava.com/oauth/token"),
    )

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def request(self, **kwargs):
            return unauthorized

        async def post(self, *args, **kwargs):
            return refreshed

    monkeypatch.setattr(
        "app.services.strava_service.httpx.AsyncClient", FakeAsyncClient
    )
    service = StravaService()

    with pytest.raises(StravaAPIError, match="refreshed access token"):
        await service._make_request("GET", "/athlete")


@pytest.mark.asyncio
async def test_concurrent_services_refresh_shared_token_only_once(
    tmp_path, monkeypatch
):
    token_file = tmp_path / "shared-tokens.json"
    monkeypatch.setattr(settings, "strava_client_id", "shared-client")
    monkeypatch.setattr(settings, "strava_client_secret", "shared-secret")
    monkeypatch.setattr(settings, "strava_access_token", "expired-access")
    monkeypatch.setattr(settings, "strava_refresh_token", "initial-refresh")
    monkeypatch.setattr(settings, "strava_token_expires_at", 0)
    monkeypatch.setattr(settings, "strava_token_file", str(token_file))

    refresh_count = 0

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            nonlocal refresh_count
            refresh_count += 1
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={
                    "access_token": "shared-new-access",
                    "refresh_token": "shared-new-refresh",
                    "expires_at": int(time.time()) + 21_600,
                },
                request=httpx.Request("POST", "https://www.strava.com/oauth/token"),
            )

    monkeypatch.setattr(
        "app.services.strava_service.httpx.AsyncClient", FakeAsyncClient
    )
    first_service = StravaService()
    second_service = StravaService()

    await asyncio.gather(
        first_service._ensure_valid_token(),
        second_service._ensure_valid_token(),
    )

    assert refresh_count == 1
    assert first_service.access_token == "shared-new-access"
    assert second_service.access_token == "shared-new-access"
