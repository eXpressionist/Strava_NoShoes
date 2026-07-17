"""Ensure all user-facing runtime paths use the live Strava API."""

from app.api.forecast_routes import service as forecast_api_service
from app.api.routes import service as api_service
from app.main import bot_service
from app.services.strava_service import StravaService


def test_runtime_services_use_strava_api():
    assert isinstance(api_service, StravaService)
    assert isinstance(forecast_api_service.activities, StravaService)
    assert isinstance(bot_service.activity_service, StravaService)
