import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.main import app


@pytest.fixture
def dashboard_password(monkeypatch):
    password = "Пароль +& test"
    monkeypatch.setattr(settings, "dashboard_password", SecretStr(password))
    monkeypatch.setattr(
        settings, "dashboard_session_secret", SecretStr("test-signing-secret")
    )
    monkeypatch.setattr(settings, "dashboard_cookie_secure", True)
    return password


@pytest.fixture
def dashboard_client():
    client = TestClient(app, base_url="https://testserver")
    client.follow_redirects = False
    return client


@pytest.fixture
def signed_in_client(dashboard_client, dashboard_password):
    response = dashboard_client.post("/login", data={"password": dashboard_password})
    assert response.status_code == 303
    return dashboard_client
