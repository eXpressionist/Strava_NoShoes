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