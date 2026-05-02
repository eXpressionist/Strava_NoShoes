"""Basic tests for the main application."""

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health_check():
    """Test the health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["app"] == "Strava NoShoes"


def test_root_endpoint():
    """Test the root endpoint returns HTML."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_custom_ranges_wait_for_dates_before_loading():
    """Custom ranges should not fetch all history before dates are selected."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "function hasCompleteCustomRange(afterId, beforeId)" in html
    assert (
        "if (range === 'custom' && !hasCompleteCustomRange('stats-after-date', "
        "'stats-before-date'))"
    ) in html
    assert (
        "if (range === 'custom' && !hasCompleteCustomRange('after-date', "
        "'before-date'))"
    ) in html


def test_activity_cards_show_weekday_dates():
    """Activity cards should include weekday-aware date formatting."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "function formatActivityDateWithWeekday(dateValue)" in html
    assert "weekday: 'short'" in html
    assert "${formatActivityDateWithWeekday(activity.start_date)}" in html


def test_preset_activity_ranges_include_current_day():
    """Activity requests should convert end dates to exclusive upper bounds."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "function toExclusiveBeforeDateKey(dateKey)" in html
    assert "beforeDate.setDate(beforeDate.getDate() + 1);" in html
    assert (
        "url += `&before=${toExclusiveBeforeDateKey(dateBounds.before)}`"
    ) in html


def test_custom_stats_range_includes_end_date():
    """Custom stats requests should convert end dates to exclusive upper bounds."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "if (before) url += `&before=${toExclusiveBeforeDateKey(before)}`;" in html


def test_calendar_view_mode_is_available():
    """The activity list should support an optional calendar view."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert 'id="view-mode"' in html
    assert 'value="calendar"' in html
    assert "function displayCalendarActivities(activities" in html
    assert "calendar-grid" in html
    assert "strava_view_mode" in html


def test_calendar_activity_meta_uses_ascii_separator():
    """Calendar activity labels should not render mojibake separators."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "вЂў" not in html
    assert "${typeLabel} - ${(activity.distance / 1000).toFixed(2)} km" in html


def test_api_health_check():
    """Test the API health check endpoint."""
    response = client.get("/api/v1/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_config_loading():
    """Test that configuration loads properly."""
    from app.config import settings
    
    # Test that settings object exists and has required attributes
    assert hasattr(settings, 'strava_client_id')
    assert hasattr(settings, 'strava_client_secret')
    assert hasattr(settings, 'app_host')
    assert hasattr(settings, 'app_port')


def test_models_import():
    """Test that models can be imported without errors."""
    from app.models.strava import Activity, Athlete, Gear, StravaTokens
    
    # Test that classes exist
    assert Activity is not None
    assert Athlete is not None
    assert Gear is not None
    assert StravaTokens is not None


def test_services_import():
    """Test that services can be imported without errors."""
    from app.services.strava_service import StravaService
    
    # Test that service class exists
    assert StravaService is not None
    
    # Test that service can be instantiated
    service = StravaService()
    assert service is not None
