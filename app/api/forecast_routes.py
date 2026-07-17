"""API routes for trail race planning and checkpoint ETA forecasts."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app.models.forecast import (
    ForecastActivityCandidate,
    ForecastRequest,
    ForecastResponse,
    RoutePreview,
)
from app.services.forecast_service import ForecastService, ForecastServiceError
from app.services.strava_service import StravaAPIError


router = APIRouter(prefix="/race-forecast")
service = ForecastService()


@router.get("/activities", response_model=List[ForecastActivityCandidate])
async def get_forecast_activities(
    after: Optional[datetime] = Query(None),
    before: Optional[datetime] = Query(None),
    min_distance_km: float = Query(15.0, ge=0),
    min_elevation_gain_m: float = Query(300.0, ge=0),
):
    """Return long trail runs suitable for historical calibration."""
    try:
        return await service.get_candidates(
            after=after,
            before=before,
            min_distance_km=min_distance_km,
            min_elevation_gain_m=min_elevation_gain_m,
        )
    except (ForecastServiceError, StravaAPIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/gpx", response_model=RoutePreview)
async def upload_planned_route(file: UploadFile = File(...)):
    """Store and parse a planned GPX route, including named waypoints."""
    filename = file.filename or "route.gpx"
    if not filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Only GPX files are supported")
    content = await file.read(service.max_upload_bytes + 1)
    try:
        return service.store_route(content, filename)
    except ForecastServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/calculate", response_model=ForecastResponse)
async def calculate_forecast(request: ForecastRequest):
    """Calculate elapsed-time ranges for checkpoints and finish."""
    try:
        return await service.calculate(request)
    except (ForecastServiceError, StravaAPIError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
