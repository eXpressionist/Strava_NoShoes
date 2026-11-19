"""Password sessions and API token authorization for the dashboard."""

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import SecretStr
from starlette.middleware.base import RequestResponseEndpoint

from app.config import settings

DASHBOARD_PAGES = {"/", "/race-forecast", "/docs", "/redoc"}
PUBLIC_PATHS = {"/health", "/api/v1", "/api/v1/", "/static/favicon.svg"}
SESSION_SECONDS = 12 * 60 * 60


def matches_secret(value: str, secret: SecretStr) -> bool:
    expected = secret.get_secret_value()
    return bool(expected) and hmac.compare_digest(value.encode(), expected.encode())


def dashboard_is_configured() -> bool:
    return bool(
        settings.dashboard_password.get_secret_value()
        and settings.dashboard_session_secret.get_secret_value()
    )


def _cookie_name() -> str:
    # Cookies are shared across ports. Separate signing secrets keep instances apart.
    secret = settings.dashboard_session_secret.get_secret_value()
    return "strava_session_" + hashlib.sha256(secret.encode()).hexdigest()[:16]


def _signature(payload: str) -> str:
    secret = settings.dashboard_session_secret.get_secret_value().encode()
    password = settings.dashboard_password.get_secret_value().encode()
    # Changing the password also invalidates existing cookies.
    key = hmac.digest(secret, b"dashboard-session\0" + password, "sha256")
    return hmac.digest(key, payload.encode(), "sha256").hex()


def has_dashboard_session(request: Request) -> bool:
    if not dashboard_is_configured():
        return False
    cookie = request.cookies.get(_cookie_name(), "")
    payload, separator, signature = cookie.rpartition(".")
    if not separator or not hmac.compare_digest(
        signature.encode(), _signature(payload).encode()
    ):
        return False
    expires_at, _, _ = payload.partition(".")
    return int(expires_at) > time.time()


def set_dashboard_session(request: Request, response: Response) -> None:
    expires_at = int(time.time()) + SESSION_SECONDS
    payload = f"{expires_at}.{secrets.token_urlsafe(24)}"
    response.set_cookie(
        _cookie_name(),
        f"{payload}.{_signature(payload)}",
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=settings.dashboard_cookie_secure or request.url.scheme == "https",
        samesite="lax",
    )


def clear_dashboard_session(request: Request, response: Response) -> None:
    response.delete_cookie(
        _cookie_name(),
        httponly=True,
        secure=settings.dashboard_cookie_secure or request.url.scheme == "https",
    )


async def protect_dashboard(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    api_authorized = (
        path.startswith("/api/v1/")
        and scheme.lower() == "bearer"
        and matches_secret(token, settings.dashboard_api_token)
    )
    page = path.rstrip("/") or "/"
    if (
        path in {"/login", "/logout"}
        or api_authorized
        or has_dashboard_session(request)
    ):
        response = await call_next(request)
    elif page in DASHBOARD_PAGES and request.method in {"GET", "HEAD"}:
        response = RedirectResponse(
            f"/login?{urlencode({'next': page})}", status_code=303
        )
    else:
        response = JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    response.headers["Cache-Control"] = "no-store"
    return response
