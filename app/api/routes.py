import os
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse
from app.models.strava import Activity, Athlete, Gear, ActivityFilter, GPXDownloadRequest, PaginatedResponse
from app.services.strava_service import StravaService, StravaAPIError

router = APIRouter()
strava_service = StravaService()


@router.get("/", summary="Health check")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "message": "Strava NoShoes API is running"}


@router.get("/athlete", response_model=Athlete, summary="Get athlete information")
async def get_athlete():
    """Get the authenticated athlete's information."""
    try:
        athlete = await strava_service.get_athlete()
        return athlete
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activities", response_model=PaginatedResponse, summary="Get athlete activities")
async def get_activities(
    before: Optional[datetime] = Query(None, description="Activities before this date"),
    after: Optional[datetime] = Query(None, description="Activities after this date"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(30, ge=1, le=200, description="Activities per page"),
    activity_type: Optional[str] = Query(None, description="Filter by activity type (Run, Ride, etc.)"),
    has_gear: Optional[bool] = Query(None, description="Filter activities with/without gear"),
    gear_id: Optional[str] = Query(None, description="Filter by specific gear ID")
):
    """Get athlete's activities with optional filtering."""
    try:
        activity_filter = ActivityFilter(
            before=before,
            after=after,
            page=page,
            per_page=per_page,
            activity_type=activity_type,
            has_gear=has_gear,
            gear_id=gear_id
        )
        
        # Always fetch all matching activities to determine total count for pagination
        # This might be optimized later with caching or separate count endpoints
        fetched_activities = await strava_service.get_activities(activity_filter, all_pages=True)
        
        # Explicit client-side date filtering (backup for API consistency)
        if after:
            # Simple timestamp comparison for robustness
            cutoff = after.timestamp()
            fetched_activities = [a for a in fetched_activities if a.start_date.timestamp() > cutoff]
        
        total = len(fetched_activities)
        total_pages = (total + per_page - 1) // per_page
        
        # Apply pagination in memory
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_items = fetched_activities[start_idx:end_idx]
        
        return {
            "items": paginated_items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages
        }
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activities/{activity_id}", response_model=Activity, summary="Get activity by ID")
async def get_activity(activity_id: int):
    """Get detailed information about a specific activity."""
    try:
        activity = await strava_service.get_activity_by_id(activity_id)
        return activity
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activities/no-gear", response_model=List[Activity], summary="Get activities without gear")
async def get_activities_without_gear():
    """Get all activities that don't have gear assigned."""
    try:
        activities = await strava_service.get_activities_without_gear()
        return activities
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activities/running", response_model=List[Activity], summary="Get running activities")
async def get_running_activities(
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Limit number of activities")
):
    """Get running activities specifically."""
    try:
        activities = await strava_service.get_running_activities(limit)
        return activities
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/gear", response_model=List[Gear], summary="Get athlete gear")
async def get_gear():
    """Get athlete's gear list."""
    try:
        gear = await strava_service.get_athlete_gear()
        return gear
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/activities/{activity_id}/download-gpx", summary="Download GPX file")
async def download_gpx(
    activity_id: int,
    background_tasks: BackgroundTasks,
    activity_name: Optional[str] = Query(None, description="Activity name for filename")
):
    """Download GPX file for an activity."""
    try:
        file_path = await strava_service.download_gpx(activity_id, activity_name=activity_name)
        return {
            "message": f"GPX file downloaded successfully",
            "file_path": file_path,
            "activity_id": activity_id
        }
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/activities/{activity_id}/gpx", summary="Get GPX file")
async def get_gpx_file(
    activity_id: int,
    activity_name: Optional[str] = Query(None, description="Activity name for filename")
):
    """Serve the downloaded GPX file."""
    try:
        # First try to download if not exists
        file_path = await strava_service.download_gpx(activity_id, activity_name=activity_name)
        return FileResponse(
            path=file_path,
            media_type='application/gpx+xml',
            filename=os.path.basename(file_path)
        )
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="GPX file not found")


@router.get("/stats/summary", summary="Get activity statistics")
async def get_activity_stats(
    range: Optional[str] = Query(None, description="Time range (1w, 1m, 3m, 1y, all)"),
    after: Optional[datetime] = Query(None, description="Custom after date"),
    before: Optional[datetime] = Query(None, description="Custom before date")
):
    """Get summary statistics of activities."""
    try:
        if range:
            from datetime import timedelta
            now = datetime.now()
            if range == "1w":
                after = now - timedelta(weeks=1)
            elif range == "1m":
                after = now - timedelta(days=30)
            elif range == "3m":
                after = now - timedelta(days=90)
            elif range == "1y":
                after = now - timedelta(days=365)
            elif range == "all":
                after = None
                before = None

        # Fetch all activities for accurate stats
        activity_filter = ActivityFilter(after=after, before=before)
        activities = await strava_service.get_activities(activity_filter, all_pages=True)
        
        total_activities = len(activities)
        total_distance = sum(activity.distance for activity in activities)
        total_time = sum(activity.moving_time for activity in activities)
        activities_without_gear = len([a for a in activities if a.gear_id is None])
        
        activity_types = {}
        activity_type_details = {}
        
        for activity in activities:
            a_type = activity.sport_type
            # Count
            activity_types[a_type] = activity_types.get(a_type, 0) + 1
            
            # Detailed stats
            if a_type not in activity_type_details:
                activity_type_details[a_type] = {
                    "count": 0,
                    "distance_meters": 0.0,
                    "distance_km": 0.0,
                    "time_seconds": 0,
                    "time_hours": 0.0
                }
            
            details = activity_type_details[a_type]
            details["count"] += 1
            details["distance_meters"] += activity.distance
            details["time_seconds"] += activity.moving_time

        # Finalize rounding and conversions
        for a_type, details in activity_type_details.items():
            details["distance_km"] = round(details["distance_meters"] / 1000, 2)
            details["time_hours"] = round(details["time_seconds"] / 3600, 2)
            # Distance meters and time seconds are already ints/floats
        
        return {
            "total": {
                "count": total_activities,
                "distance_meters": total_distance,
                "distance_km": round(total_distance / 1000, 2),
                "time_seconds": total_time,
                "time_hours": round(total_time / 3600, 2),
                "activities_without_gear": activities_without_gear,
            },
            "activity_types_count": activity_types,
            "activity_types_detailed": activity_type_details
        }
    except StravaAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))