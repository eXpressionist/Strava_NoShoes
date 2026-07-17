"""Main FastAPI application."""

import inspect
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes import router as api_router
from app.api.forecast_routes import router as forecast_router
from app.config import settings

from contextlib import asynccontextmanager
from app.services.bot_service import BotService

# Bot service instance
bot_service = BotService()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize the Telegram bot. Activity data comes from Strava API.
    await bot_service.initialize()
    yield
    # Shutdown: Stop bot
    await bot_service.shutdown()

# Create FastAPI app
app = FastAPI(
    title="Strava NoShoes",
    description="Modern Python application for Strava API integration with activity management and GPX track downloads",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# Create necessary directories
Path(settings.gpx_storage_path).mkdir(parents=True, exist_ok=True)
Path("app/static").mkdir(parents=True, exist_ok=True)
Path("app/templates").mkdir(parents=True, exist_ok=True)

# Mount static files
if os.path.exists("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Setup templates
templates = Jinja2Templates(directory="app/templates")


def render_template(request: Request, name: str):
    """Render templates across both supported Starlette calling conventions."""
    parameters = inspect.signature(templates.TemplateResponse).parameters
    context = {"request": request}
    if "request" in parameters:
        return templates.TemplateResponse(request=request, name=name, context=context)
    return templates.TemplateResponse(name, context)

# Include API routes
app.include_router(api_router, prefix="/api/v1", tags=["Strava API"])
app.include_router(forecast_router, prefix="/api/v1", tags=["Race Forecast"])


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
    return {
        "status": "healthy",
        "app": "Strava NoShoes",
        "version": "0.1.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug
    )
