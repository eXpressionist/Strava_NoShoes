"""Strava API service for handling all Strava-related operations."""

import asyncio
import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
import httpx
from app.config import settings
from app.models.strava import Activity, Athlete, Gear, StravaTokens, ActivityFilter

import gpxpy
import gpxpy.gpx


class StravaAPIError(Exception):
    """Custom exception for Strava API errors."""
    pass


class StravaService:
    """Service class for interacting with Strava API."""
    
    def __init__(self):
        self.base_url = settings.strava_api_base_url
        self.client_id = settings.strava_client_id
        self.client_secret = settings.strava_client_secret
        self.token_file = settings.strava_token_file
        
        # Initialize tokens from settings (defaults)
        self.access_token = settings.strava_access_token
        self.refresh_token = settings.strava_refresh_token
        self.expires_at = settings.strava_token_expires_at
        
        # Try to load from token file
        self._load_tokens()
        
        self._gear_cache: Dict[str, str] = {}
        
    def _load_tokens(self) -> None:
        """Load tokens from local file if it exists."""
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r") as f:
                    data = json.load(f)
                    self.access_token = data.get("access_token", self.access_token)
                    self.refresh_token = data.get("refresh_token", self.refresh_token)
                    self.expires_at = data.get("expires_at", self.expires_at)
            except Exception as e:
                print(f"Error loading tokens from {self.token_file}: {e}")

    def _save_tokens(self) -> None:
        """Save current tokens to local file."""
        os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
        try:
            with open(self.token_file, "w") as f:
                json.dump({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "expires_at": self.expires_at
                }, f, indent=4)
        except Exception as e:
            print(f"Error saving tokens to {self.token_file}: {e}")

    async def _ensure_valid_token(self) -> None:
        """Check if token is expired or expiring soon and refresh if needed."""
        # Refresh if token expires in less than 60 minutes
        if not self.access_token or not self.expires_at or self.expires_at < time.time() + 3600:
            print(f"Strava token expiring soon or missing (expires_at: {self.expires_at}). Refreshing...")
            await self._refresh_access_token()

    async def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers for API requests."""
        await self._ensure_valid_token()
        
        if not self.access_token:
            raise StravaAPIError("Access token not available. Please authenticate first.")
        
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
    
    async def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Make an authenticated request to Strava API."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = await self._get_headers()
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=30.0
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    # Try to refresh token
                    if await self._refresh_access_token():
                        # Retry the request with new token
                        headers = await self._get_headers()
                        response = await client.request(
                            method=method,
                            url=url,
                            headers=headers,
                            params=params,
                            json=json_data,
                            timeout=30.0
                        )
                        response.raise_for_status()
                        return response.json()
                    else:
                        raise StravaAPIError("Authentication failed. Please re-authenticate.")
                else:
                    raise StravaAPIError(f"API request failed: {e.response.status_code} - {e.response.text}")
            except httpx.RequestError as e:
                raise StravaAPIError(f"Request error: {str(e)}")
    
    async def _refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token."""
        if not self.refresh_token:
            return False
        
        url = "https://www.strava.com/oauth/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token"
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, data=data)
                response.raise_for_status()
                token_data = response.json()
                
                self.access_token = token_data["access_token"]
                self.refresh_token = token_data["refresh_token"]
                self.expires_at = token_data["expires_at"]
                
                # Update settings (syncing with settings object if needed)
                settings.strava_access_token = self.access_token
                settings.strava_refresh_token = self.refresh_token
                settings.strava_token_expires_at = self.expires_at
                
                # Persist to file
                self._save_tokens()
                
                return True
            except (httpx.HTTPStatusError, httpx.RequestError, KeyError):
                return False
    
    async def get_athlete(self) -> Athlete:
        """Get the authenticated athlete's information."""
        data = await self._make_request("GET", "/athlete")
        return Athlete(**data)
    
    async def get_activities(
        self, 
        activity_filter: Optional[ActivityFilter] = None,
        all_pages: bool = False
    ) -> List[Activity]:
        """Get athlete's activities with optional filtering."""
        params = {}
        
        if activity_filter:
            if activity_filter.before:
                params["before"] = int(activity_filter.before.timestamp())
            if activity_filter.after:
                params["after"] = int(activity_filter.after.timestamp())
            params["page"] = activity_filter.page
            params["per_page"] = activity_filter.per_page
        
        print(f"DEBUG: get_activities params: {params} | filter: {activity_filter}")
        
        all_activities_data = []
        
        if all_pages:
            params["per_page"] = 200  # Maximize items per page for efficiency
            current_page = 1
            while True:
                params["page"] = current_page
                data = await self._make_request("GET", "/athlete/activities", params=params)
                if not data:
                    break
                all_activities_data.extend(data)
                if len(data) < 200:
                    break
                current_page += 1
        else:
            data = await self._make_request("GET", "/athlete/activities", params=params)
            all_activities_data = data
            
        activities = [Activity(**activity_data) for activity_data in all_activities_data]
        
        # Populate gear names using the new method
        if activities:
            await self._populate_gear_names_for_activities(activities)

        # Apply additional filters
        if activity_filter:
            if activity_filter.activity_type:
                activities = [a for a in activities if a.sport_type == activity_filter.activity_type]
            
            if activity_filter.has_gear is not None:
                if activity_filter.has_gear:
                    activities = [a for a in activities if a.gear_id is not None]
                else:
                    activities = [a for a in activities if a.gear_id is None]
            
            if activity_filter.gear_id:
                activities = [a for a in activities if a.gear_id == activity_filter.gear_id]

            # Explicitly filter by date (client-side backup)
            if activity_filter.after:
                # Ensure we compare timezone-aware datetimes if possible
                if activity_filter.after.tzinfo is None and activities and activities[0].start_date.tzinfo:
                    # Make filter aware (assume UTC/local match or just naive compare)
                    # Simplest: use timestamp comparison
                    cutoff = activity_filter.after.timestamp()
                    activities = [a for a in activities if a.start_date.timestamp() > cutoff]
                else:
                    activities = [a for a in activities if a.start_date > activity_filter.after]
        
        # Always sort by date descending (newest first)
        
        # Always sort by date descending (newest first)
        # Strava API returns ascending if 'after' is used, so we force consistency
        activities.sort(key=lambda x: x.start_date, reverse=True)
        
        return activities
    
    async def get_activity_by_id(self, activity_id: int) -> Activity:
        """Get detailed information about a specific activity."""
        data = await self._make_request("GET", f"/activities/{activity_id}")
        return Activity(**data)
    
    async def get_gear_by_id(self, gear_id: str) -> Optional[Gear]:
        """Get detailed gear information by ID."""
        try:
            gear_data = await self._make_request("GET", f"/gear/{gear_id}")
            return Gear(**gear_data)
        except Exception as e:
            print(f"Error fetching gear {gear_id}: {e}")
            return None

    async def get_athlete_gear(self) -> List[Gear]:
        """Get athlete's gear list by collecting unique gear IDs from activities."""
        try:
            # Strava API doesn't return shoes/bikes in /athlete endpoint
            # We need to collect gear IDs from activities and fetch details individually
            print("Fetching activities to collect gear IDs...")
            
            # Get recent activities to find gear IDs
            activities_data = await self._make_request("GET", "/athlete/activities", params={"per_page": 200})
            
            # Collect unique gear IDs
            gear_ids = set()
            for activity in activities_data:
                if activity.get('gear_id'):
                    gear_ids.add(activity['gear_id'])
            
            print(f"Found {len(gear_ids)} unique gear IDs from activities")
            
            # Fetch details for each gear
            gear_list = []
            for gear_id in gear_ids:
                gear = await self.get_gear_by_id(gear_id)
                if gear:
                    gear_list.append(gear)
                    self._gear_cache[gear.id] = gear.name
                    print(f"Cached gear: {gear.id} -> {gear.name}")
            
            print(f"Total gear cached: {len(self._gear_cache)} items")
            return gear_list
            
        except Exception as e:
            print(f"Error fetching athlete gear: {e}")
            import traceback
            traceback.print_exc()
            return []

    async def _get_gear_map(self) -> Dict[str, str]:
        """Get mapping of gear ID to name, using cache if available."""
        if not self._gear_cache:
            print("Gear cache empty, fetching gear...")
            await self.get_athlete_gear()
        else:
            print(f"Using cached gear: {len(self._gear_cache)} items")
        return self._gear_cache
    
    async def _populate_gear_names_for_activities(self, activities: List[Activity]) -> None:
        """Populate gear names for activities by fetching missing gear details."""
        # Collect gear IDs that need names
        missing_gear_ids = set()
        for activity in activities:
            if activity.gear_id and activity.gear_id not in self._gear_cache:
                missing_gear_ids.add(activity.gear_id)
        
        # Fetch missing gear details
        if missing_gear_ids:
            print(f"Fetching details for {len(missing_gear_ids)} gear items...")
            for gear_id in missing_gear_ids:
                gear = await self.get_gear_by_id(gear_id)
                if gear:
                    self._gear_cache[gear.id] = gear.name
                    print(f"Cached gear: {gear.id} -> {gear.name}")
        
        # Apply gear names to activities
        for activity in activities:
            if activity.gear_id and activity.gear_id in self._gear_cache:
                activity.gear_name = self._gear_cache[activity.gear_id]
    
    async def get_activity_streams(self, activity_id: int) -> Dict[str, Any]:
        """Get activity streams for GPX generation."""
        keys = ["latlng", "altitude", "time", "heartrate", "cadence", "temp"]
        endpoint = f"/activities/{activity_id}/streams"
        params = {
            "keys": ",".join(keys),
            "key_by_type": "true"
        }
        return await self._make_request("GET", endpoint, params=params)
    
    async def download_gpx(self, activity_id: int, save_path: Optional[str] = None, activity_name: Optional[str] = None) -> str:
        """Download GPX file by fetching streams and manually constructing the GPX file."""
        if not save_path:
            save_path = settings.gpx_storage_path
        
        # Create directory if it doesn't exist
        Path(save_path).mkdir(parents=True, exist_ok=True)
        
        # Get activity details
        activity = await self.get_activity_by_id(activity_id)
        if not activity_name:
            activity_name = activity.name
        
        # Check if file already exists (caching)
        safe_name = activity_name.replace(' ', '_').replace('/', '_').replace('\\', '_')
        filename = f"{safe_name}.gpx"
        file_path = os.path.join(save_path, filename)
        
        if os.path.exists(file_path):
            # File already downloaded, return cached version
            return file_path
        
        # Fetch streams
        streams = await self.get_activity_streams(activity_id)
        
        if "latlng" not in streams:
            raise StravaAPIError(f"No GPS data (latlng stream) available for activity {activity_id}")
        
        # Construct GPX
        gpx = gpxpy.gpx.GPX()
        gpx.name = activity_name
        gpx.description = f"Strava Activity {activity_id}"
        
        # Create track
        gpx_track = gpxpy.gpx.GPXTrack()
        gpx.tracks.append(gpx_track)
        
        # Create segment
        gpx_segment = gpxpy.gpx.GPXTrackSegment()
        gpx_track.segments.append(gpx_segment)
        
        # Get stream data
        latlngs = streams["latlng"]["data"]
        times = streams.get("time", {}).get("data", [])
        altitudes = streams.get("altitude", {}).get("data", [])
        heartrates = streams.get("heartrate", {}).get("data", [])
        cadences = streams.get("cadence", {}).get("data", [])
        
        start_time = activity.start_date  # This is a datetime object
        
        for i in range(len(latlngs)):
            lat, lon = latlngs[i]
            
            # Calculate timestamp
            point_time = None
            if i < len(times):
                from datetime import timedelta
                point_time = start_time + timedelta(seconds=times[i])
            
            elevation = altitudes[i] if i < len(altitudes) else None
            
            point = gpxpy.gpx.GPXTrackPoint(
                latitude=lat,
                longitude=lon,
                elevation=elevation,
                time=point_time
            )
            
            # Add extensions for heartrate and cadence
            if i < len(heartrates) or i < len(cadences):
                from lxml import etree
                TPE_NS = 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1'
                tpx = etree.Element(f'{{{TPE_NS}}}TrackPointExtension')
                
                if i < len(heartrates):
                    hr = etree.SubElement(tpx, f'{{{TPE_NS}}}hr')
                    hr.text = str(heartrates[i])
                
                if i < len(cadences):
                    cad = etree.SubElement(tpx, f'{{{TPE_NS}}}cad')
                    cad.text = str(cadences[i])
                
                point.extensions.append(tpx)
            
            gpx_segment.points.append(point)
        
        # Save GPX file with structural fixes
        gpx_xml = gpx.to_xml()
        
        # Precise string replacement to fix Garmin extensions and namespaces
        TPE_NS = 'http://www.garmin.com/xmlschemas/TrackPointExtension/v1'
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:TrackPointExtension>', '<gpxtpx:TrackPointExtension>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:TrackPointExtension>', '</gpxtpx:TrackPointExtension>')
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:hr>', '<gpxtpx:hr>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:hr>', '</gpxtpx:hr>')
        gpx_xml = gpx_xml.replace(f'<{TPE_NS}:cad>', '<gpxtpx:cad>')
        gpx_xml = gpx_xml.replace(f'</{TPE_NS}:cad>', '</gpxtpx:cad>')
        
        if 'xmlns:gpxtpx' not in gpx_xml:
            gpx_xml = gpx_xml.replace('<gpx ', f'<gpx xmlns:gpxtpx="{TPE_NS}" ')
            
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(gpx_xml)
        
        return file_path
    
    async def get_activities_without_gear(self, after: Optional[datetime] = None) -> List[Activity]:
        """Get all activities that don't have gear assigned, optionally after a specific date."""
        activity_filter = ActivityFilter(has_gear=False, after=after)
        return await self.get_activities(activity_filter, all_pages=True)
    
    async def get_running_activities(self, limit: Optional[int] = None) -> List[Activity]:
        """Get running activities specifically."""
        if limit and limit <= 200:
            activity_filter = ActivityFilter(activity_type="Run", per_page=limit)
            return await self.get_activities(activity_filter)
        
        activity_filter = ActivityFilter(activity_type="Run")
        activities = await self.get_activities(activity_filter, all_pages=True)
        
        if limit and len(activities) > limit:
            return activities[:limit]
        
        return activities