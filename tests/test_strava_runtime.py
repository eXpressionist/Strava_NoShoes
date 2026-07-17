"""Ensure all user-facing runtime paths use the live Strava API."""

import json

import httpx
import pytest

from app.api.forecast_routes import service as forecast_api_service
from app.api.routes import service as api_service
from app.config import settings
from app.main import bot_service
from app.services.strava_service import StravaAPIError, StravaService


def test_runtime_services_use_strava_api():
    assert isinstance(api_service, StravaService)
    assert isinstance(forecast_api_service.activities, StravaService)
    assert isinstance(bot_service.activity_service, StravaService)


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
