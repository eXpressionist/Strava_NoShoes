"""Main FastAPI application."""

import inspect
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.forecast_routes import router as forecast_router
from app.api.routes import router as api_router
from app.config import settings
from app.services.backup_service import backup_scheduler
from app.services.bot_service import BotService
from app.services.garmin_health_service import garmin_health_scheduler
from app.utils.dashboard_auth import (
    DASHBOARD_PAGES,
    clear_dashboard_session,
    dashboard_is_configured,
    has_dashboard_session,
    matches_secret,
    protect_dashboard,
    set_dashboard_session,
)

# Bot service instance
bot_service = BotService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Keep the API responsive while the resumable backup runs in the background.
    await garmin_health_scheduler.start()
    await backup_scheduler.start()
    await bot_service.initialize()
    yield
    await backup_scheduler.shutdown()
    await garmin_health_scheduler.shutdown()
    await bot_service.shutdown()


# Create FastAPI app
app = FastAPI(
    title="Strava NoShoes",
    description="Modern Python application for Strava API integration with activity management and GPX track downloads",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)
app.middleware("http")(protect_dashboard)

# Create necessary directories
Path(settings.gpx_storage_path).mkdir(parents=True, exist_ok=True)
Path("app/static").mkdir(parents=True, exist_ok=True)
Path("app/templates").mkdir(parents=True, exist_ok=True)

# Mount static files
if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Setup templates
templates = Jinja2Templates(directory="app/templates")


def render_template(
    request: Request, name: str, *, status_code: int = 200, **context: object
):
    """Render templates across both supported Starlette calling conventions."""
    parameters = inspect.signature(templates.TemplateResponse).parameters
    context["request"] = request
    if "request" in parameters:
        return templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )
    return templates.TemplateResponse(name, context, status_code=status_code)


# Include API routes
app.include_router(api_router, prefix="/api/v1", tags=["Strava API"])
app.include_router(forecast_router, prefix="/api/v1", tags=["Race Forecast"])


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, destination: str = Query("/", alias="next")):
    if destination not in DASHBOARD_PAGES:
        destination = "/"
    if not dashboard_is_configured():
        return render_template(request, "login.html", status_code=503, unavailable=True)
    if has_dashboard_session(request):
        return RedirectResponse(destination, status_code=303)
    return render_template(request, "login.html", destination=destination)


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login(request: Request):
    if not dashboard_is_configured():
        return render_template(request, "login.html", status_code=503, unavailable=True)

    content_type = request.headers.get("content-type", "").partition(";")[0].strip()
    if content_type.lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Expected a URL-encoded form")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise HTTPException(413, "Login form is too large")
        body.extend(chunk)
    try:
        form = parse_qs(body.decode("ascii"), max_num_fields=2)
    except ValueError as exc:
        raise HTTPException(400, "Invalid login form") from exc

    destination = form.get("next", ["/"])[0]
    if destination not in DASHBOARD_PAGES:
        destination = "/"
    if not matches_secret(form.get("password", [""])[0], settings.dashboard_password):
        return render_template(
            request,
            "login.html",
            status_code=401,
            destination=destination,
            invalid=True,
        )

    response = RedirectResponse(destination, status_code=303)
    set_dashboard_session(request, response)
    return response


@app.post("/logout", include_in_schema=False)
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    if has_dashboard_session(request):
        clear_dashboard_session(request, response)
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_root(request: Request):
    """Serve the main web interface."""
    return render_template(request, "index.html")


@app.get("/race-forecast", response_class=HTMLResponse, include_in_schema=False)
async def race_forecast_page(request: Request):
    """Serve the trail race forecast interface."""
    return render_template(request, "race_forecast.html")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "app": "Strava NoShoes", "version": "0.1.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )
