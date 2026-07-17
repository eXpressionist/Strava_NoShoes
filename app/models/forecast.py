"""Models for trail race forecasting."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class ForecastActivityCandidate(BaseModel):
    """Historical activity that can be used to calibrate a forecast."""

    id: int
    source: str
    name: str
    start_date: datetime
    sport_type: str
    distance_km: float
    elevation_gain_m: float
    moving_time: int
    elapsed_time: int
    suggested_race: bool = False


class HistoricalActivitySelection(BaseModel):
    """User selection and explicit race classification."""

    activity_id: int
    source: str = Field(pattern="^(strava|garmin)$")
    is_race: bool = False


class CheckpointInput(BaseModel):
    """An imported or manually entered checkpoint."""

    name: str = Field(min_length=1, max_length=120)
    distance_km: float = Field(ge=0)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)


class RouteCheckpoint(CheckpointInput):
    """Checkpoint enriched with route data."""

    elevation_m: Optional[float] = None
    source: str = Field(default="gpx", pattern="^(gpx|manual|system)$")


class RoutePreview(BaseModel):
    """Summary of an uploaded GPX route."""

    route_id: str
    name: str
    distance_km: float
    elevation_gain_m: float
    elevation_loss_m: float
    checkpoints: List[RouteCheckpoint]


class ForecastRequest(BaseModel):
    """Parameters selected by the user for one forecast calculation."""

    route_id: str
    activities: List[HistoricalActivitySelection] = Field(min_length=1)
    checkpoints: List[CheckpointInput] = Field(default_factory=list)
    start_time: Optional[datetime] = None

    @model_validator(mode="after")
    def require_past_race(self) -> "ForecastRequest":
        if not any(activity.is_race for activity in self.activities):
            raise ValueError("At least one selected activity must be marked as a race")
        return self


class ForecastCheckpointResult(BaseModel):
    """ETA range at a checkpoint."""

    name: str
    distance_km: float
    elevation_m: Optional[float] = None
    optimistic_seconds: int
    expected_seconds: int
    conservative_seconds: int
    optimistic_at: Optional[datetime] = None
    expected_at: Optional[datetime] = None
    conservative_at: Optional[datetime] = None


class ForecastResponse(BaseModel):
    """Calculated route and checkpoint forecast."""

    route: RoutePreview
    checkpoints: List[ForecastCheckpointResult]
    moving_time_seconds: int
    stop_time_seconds: int
    expected_finish_seconds: int
    uncertainty_percent: float
    confidence: str
    activities_used: int
    races_used: int
    method: str
