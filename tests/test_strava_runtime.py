"""Ensure all user-facing runtime paths use the live Strava API."""

import json

from app.api.forecast_routes import service as forecast_api_service
from app.api.routes import service as api_service
from app.config import settings
from app.main import bot_service
from app.services.strava_service import StravaService


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
