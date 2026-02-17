"""Pydantic models for Strava API data structures."""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class StravaTokens(BaseModel):
    """Strava OAuth tokens model."""
    access_token: str
    refresh_token: str
    expires_at: int
    token_type: str = "Bearer"


class Athlete(BaseModel):
    """Strava athlete model."""
    id: int
    username: Optional[str] = None
    resource_state: int
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    bio: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    sex: Optional[str] = None
    premium: Optional[bool] = None
    summit: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    badge_type_id: Optional[int] = None
    weight: Optional[float] = None
    profile_medium: Optional[str] = None
    profile: Optional[str] = None
    friend: Optional[str] = None
    follower: Optional[str] = None


class Gear(BaseModel):
    """Strava gear model."""
    id: str
    primary: bool
    name: str
    resource_state: int
    retired: Optional[bool] = None
    distance: Optional[float] = None
    converted_distance: Optional[float] = None


class ActivityMap(BaseModel):
    """Strava activity map model."""
    id: str
    summary_polyline: Optional[str] = None
    resource_state: int


class Activity(BaseModel):
    """Strava activity model."""
    resource_state: int
    athlete: Optional[Athlete] = None
    name: str
    distance: float
    moving_time: int
    elapsed_time: int
    total_elevation_gain: float
    type: str
    sport_type: str
    workout_type: Optional[int] = None
    id: int
    start_date: datetime
    start_date_local: datetime
    timezone: str
    utc_offset: float
    location_city: Optional[str] = None
    location_state: Optional[str] = None
    location_country: Optional[str] = None
    achievement_count: int
    kudos_count: int
    comment_count: int
    athlete_count: int
    photo_count: int
    map: Optional[ActivityMap] = None
    trainer: bool
    commute: bool
    manual: bool
    private: bool
    visibility: str
    flagged: bool
    gear_id: Optional[str] = None
    gear_name: Optional[str] = None
    start_latlng: Optional[List[float]] = None
    end_latlng: Optional[List[float]] = None
    average_speed: float
    max_speed: float
    average_cadence: Optional[float] = None
    average_watts: Optional[float] = None
    weighted_average_watts: Optional[int] = None
    kilojoules: Optional[float] = None
    device_watts: Optional[bool] = None
    has_heartrate: bool
    average_heartrate: Optional[float] = None
    max_heartrate: Optional[float] = None
    heartrate_opt_out: Optional[bool] = None
    display_hide_heartrate_option: Optional[bool] = None
    elev_high: Optional[float] = None
    elev_low: Optional[float] = None
    upload_id: Optional[int] = None
    upload_id_str: Optional[str] = None
    external_id: Optional[str] = None
    from_accepted_tag: Optional[bool] = None
    pr_count: int
    total_photo_count: int
    has_kudoed: bool
    suffer_score: Optional[int] = None


class ActivityFilter(BaseModel):
    """Filter parameters for activities."""
    before: Optional[datetime] = Field(None, description="Activities before this date")
    after: Optional[datetime] = Field(None, description="Activities after this date")
    page: int = Field(1, ge=1, description="Page number")
    per_page: int = Field(30, ge=1, le=200, description="Activities per page")
    activity_type: Optional[str] = Field(None, description="Filter by activity type (Run, Ride, etc.)")
    has_gear: Optional[bool] = Field(None, description="Filter activities with/without gear")
    gear_id: Optional[str] = Field(None, description="Filter by specific gear ID")


class GPXDownloadRequest(BaseModel):
    """Request model for GPX download."""
    activity_id: int = Field(..., description="Strava activity ID")
    include_original: bool = Field(True, description="Include original GPX data")


class PaginatedResponse(BaseModel):
    """Paginated response model."""
    items: List[Activity]
    total: int
    page: int
    per_page: int
    total_pages: int