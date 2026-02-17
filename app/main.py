"""Main FastAPI application."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import os
from pathlib import Path

from app.api.routes import router as api_router
from app.config import settings

from contextlib import asynccontextmanager
from app.services.bot_service import BotService

# Bot service instance
bot_service = BotService()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize bot
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

# Include API routes
app.include_router(api_router, prefix="/api/v1", tags=["Strava API"])


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def read_root(request: Request):
    """Serve the main web interface."""
    return templates.TemplateResponse("index.html", {"request": request})


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