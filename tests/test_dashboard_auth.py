from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.main import app, backup_scheduler
from app.utils import dashboard_auth


@pytest.mark.parametrize("path", ["/", "/race-forecast", "/docs", "/redoc"])
def test_pages_require_sign_in(dashboard_client, dashboard_password, path):
    response = dashboard_client.get(path, follow_redirects=True)

    assert response.status_code == 200
    assert response.history[0].status_code == 303
    assert response.url.path == "/login"
    assert response.headers["Cache-Control"] == "no-store"
    assert 'type="password"' in response.text
    assert f'name="next" value="{path}"' in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/athlete"),
        ("GET", "/api/v1/stats/summary"),
        ("GET", "/api/v1/activities/123/gpx"),
        ("POST", "/api/v1/activities/123/download-gpx"),
        ("POST", "/api/v1/race-forecast/gpx"),
        ("GET", "/openapi.json"),
    ],
)
def test_data_requests_require_authentication(dashboard_client, method, path):
    response = dashboard_client.request(method, path)

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_invalid_password_returns_the_login_form(dashboard_client, dashboard_password):
    response = dashboard_client.post(
        "/login", data={"password": "wrong", "next": "/race-forecast"}
    )

    assert response.status_code == 401
    assert "Неверный пароль" in response.text
    assert 'name="next" value="/race-forecast"' in response.text
    assert "Set-Cookie" not in response.headers


def test_password_grants_access_to_pages_and_api(
    dashboard_client, dashboard_password, monkeypatch
):
    status = AsyncMock(return_value={"running": False})
    monkeypatch.setattr(backup_scheduler, "status", status)
    response = dashboard_client.post(
        "/login", data={"password": dashboard_password, "next": "/race-forecast"}
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/race-forecast"
    cookie = response.headers["Set-Cookie"]
    assert all(flag in cookie for flag in ["HttpOnly", "Secure", "SameSite=lax"])
    assert "Max-Age=43200" in cookie
    assert dashboard_password not in cookie
    assert dashboard_client.get("/race-forecast").status_code == 200
    assert dashboard_client.get("/docs").status_code == 200
    assert dashboard_client.get("/openapi.json").status_code == 200
    response = dashboard_client.get("/api/v1/backup/status")
    assert response.json() == {"running": False}
    assert response.headers["Cache-Control"] == "no-store"
    status.assert_awaited_once()


def test_login_ignores_external_destination(dashboard_client, dashboard_password):
    response = dashboard_client.post(
        "/login", data={"password": dashboard_password, "next": "//example.org"}
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/"


def test_password_sign_in_works_over_plain_http(dashboard_password):
    client = TestClient(app, base_url="http://testserver:8000")
    response = client.post(
        "/login", data={"password": dashboard_password}, follow_redirects=True
    )

    assert response.status_code == 200
    assert response.url.path == "/"
    assert "Secure" not in response.history[0].headers["Set-Cookie"]
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_dashboard_session_expires(signed_in_client, monkeypatch):
    expires_at = dashboard_auth.time.time() + dashboard_auth.SESSION_SECONDS + 1
    monkeypatch.setattr(
        dashboard_auth, "time", SimpleNamespace(time=lambda: expires_at)
    )

    assert signed_in_client.get("/api/v1/activities").status_code == 401


def test_dashboard_rejects_a_modified_session_cookie(signed_in_client):
    [(name, cookie)] = signed_in_client.cookies.items()
    signed_in_client.cookies.clear()
    signed_in_client.cookies.set(name, f"9{cookie}")

    assert signed_in_client.get("/api/v1/activities").status_code == 401


def test_password_change_invalidates_existing_sessions(signed_in_client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_password", SecretStr("replacement"))

    assert signed_in_client.get("/").status_code == 303


def test_logout_clears_the_session(signed_in_client):
    response = signed_in_client.post("/logout")

    assert response.status_code == 303
    assert response.headers["Location"] == "/login"
    assert "Max-Age=0" in response.headers["Set-Cookie"]
    assert signed_in_client.get("/api/v1/activities").status_code == 401


def test_missing_session_secret_leaves_dashboard_locked(
    dashboard_client, dashboard_password, monkeypatch
):
    monkeypatch.setattr(settings, "dashboard_session_secret", SecretStr(""))

    assert dashboard_client.get("/", follow_redirects=True).status_code == 503
    assert dashboard_client.get("/health").status_code == 200


def test_api_token_only_grants_access_to_the_api(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "dashboard_api_token", SecretStr("test-api-token"))
    status = AsyncMock(return_value={"running": False})
    monkeypatch.setattr(backup_scheduler, "status", status)
    path = "/api/v1/backup/status"
    headers = {"Authorization": "Bearer test-api-token"}

    assert dashboard_client.get(path).status_code == 401
    assert (
        dashboard_client.get(
            path, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    status.assert_not_awaited()
    assert dashboard_client.get(path, headers=headers).json() == {"running": False}
    assert dashboard_client.get("/", headers=headers).status_code == 303
    status.assert_awaited_once()
