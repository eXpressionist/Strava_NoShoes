"""GPX parsing and a transparent first-version trail race forecast."""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence
from uuid import UUID, uuid4

import gpxpy

from app.config import settings
from app.models.forecast import (
    CheckpointInput,
    ForecastActivityCandidate,
    ForecastCheckpointResult,
    ForecastRequest,
    ForecastResponse,
    HistoricalActivitySelection,
    RouteCheckpoint,
    RoutePreview,
)
from app.models.strava import Activity, ActivityFilter
from app.services.unified_service import UnifiedActivityService, UnifiedServiceError


class ForecastServiceError(Exception):
    """Expected validation or data error in race forecasting."""


@dataclass
class RoutePoint:
    latitude: float
    longitude: float
    elevation_m: Optional[float]
    distance_km: float = 0.0
    ascent_m: float = 0.0
    descent_m: float = 0.0
    weighted_effort: float = 0.0


@dataclass
class ParsedRoute:
    name: str
    points: List[RoutePoint]
    checkpoints: List[RouteCheckpoint]

    @property
    def distance_km(self) -> float:
        return self.points[-1].distance_km

    @property
    def elevation_gain_m(self) -> float:
        return self.points[-1].ascent_m

    @property
    def elevation_loss_m(self) -> float:
        return self.points[-1].descent_m


class ForecastService:
    """Coordinates historical activity selection, GPX parsing and prediction."""

    max_upload_bytes = 20 * 1024 * 1024

    def __init__(self, activity_service: Optional[UnifiedActivityService] = None):
        self.activities = activity_service or UnifiedActivityService()
        self.route_storage = Path(settings.gpx_storage_path) / "planned_routes"

    async def get_candidates(
        self,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
        min_distance_km: float = 15.0,
        min_elevation_gain_m: float = 300.0,
    ) -> List[ForecastActivityCandidate]:
        activity_filter = ActivityFilter(
            after=after,
            before=before,
            activity_type="TrailRun",
            per_page=200,
        )
        activities = await self.activities.get_activities(activity_filter, all_pages=True)
        candidates = []
        for activity in activities:
            if activity.distance < min_distance_km * 1000:
                continue
            if activity.total_elevation_gain < min_elevation_gain_m:
                continue
            candidates.append(self._candidate_from_activity(activity))
        return candidates

    def store_route(self, content: bytes, original_name: str) -> RoutePreview:
        if not content:
            raise ForecastServiceError("The GPX file is empty")
        if len(content) > self.max_upload_bytes:
            raise ForecastServiceError("The GPX file is larger than 20 MB")

        parsed = self.parse_gpx(content, original_name)
        route_id = str(uuid4())
        self.route_storage.mkdir(parents=True, exist_ok=True)
        (self.route_storage / f"{route_id}.gpx").write_bytes(content)
        return self._route_preview(route_id, parsed)

    def load_route(self, route_id: str) -> ParsedRoute:
        try:
            safe_id = str(UUID(route_id))
        except ValueError as exc:
            raise ForecastServiceError("Invalid route identifier") from exc

        route_path = self.route_storage / f"{safe_id}.gpx"
        if not route_path.exists():
            raise ForecastServiceError("Uploaded route was not found; upload the GPX again")
        return self.parse_gpx(route_path.read_bytes(), route_path.name)

    def parse_gpx(self, content: bytes, original_name: str = "route.gpx") -> ParsedRoute:
        try:
            text = content.decode("utf-8-sig")
            gpx = gpxpy.parse(text)
        except Exception as exc:
            # gpxpy exposes several parser-specific exception types across versions.
            raise ForecastServiceError(f"Could not parse GPX: {exc}") from exc

        raw_points = []
        for track in gpx.tracks:
            for segment in track.segments:
                raw_points.extend(segment.points)

        if not raw_points:
            for route in gpx.routes:
                raw_points.extend(route.points)

        if len(raw_points) < 2:
            raise ForecastServiceError("The GPX must contain a track with at least two points")

        points = [
            RoutePoint(point.latitude, point.longitude, point.elevation)
            for point in raw_points
        ]
        self._enrich_route_points(points)

        checkpoint_points = list(gpx.waypoints)
        for route in gpx.routes:
            checkpoint_points.extend(point for point in route.points if point.name)

        checkpoints = []
        seen = set()
        for index, waypoint in enumerate(checkpoint_points, start=1):
            nearest = min(
                points,
                key=lambda point: self._haversine_m(
                    point.latitude,
                    point.longitude,
                    waypoint.latitude,
                    waypoint.longitude,
                ),
            )
            name = (waypoint.name or waypoint.description or f"КП {index}").strip()
            identity = (name.casefold(), round(nearest.distance_km, 3))
            if identity in seen:
                continue
            seen.add(identity)
            checkpoints.append(
                RouteCheckpoint(
                    name=name,
                    distance_km=round(nearest.distance_km, 3),
                    latitude=waypoint.latitude,
                    longitude=waypoint.longitude,
                    elevation_m=waypoint.elevation or nearest.elevation_m,
                    source="gpx",
                )
            )

        checkpoints.sort(key=lambda checkpoint: checkpoint.distance_km)
        route_name = gpx.name or next(
            (track.name for track in gpx.tracks if track.name),
            Path(original_name).stem,
        )
        return ParsedRoute(route_name, points, checkpoints)

    async def calculate(self, request: ForecastRequest) -> ForecastResponse:
        route = self.load_route(request.route_id)
        selected = []
        for selection in request.activities:
            try:
                activity = await self.activities.get_activity_by_id(
                    selection.activity_id, source=selection.source
                )
            except UnifiedServiceError as exc:
                raise ForecastServiceError(str(exc)) from exc
            selected.append((activity, selection))

        if not any(selection.is_race for _, selection in selected):
            raise ForecastServiceError("Mark at least one selected activity as a race")

        total_effort = self._route_effort(route)
        elapsed_samples = []
        moving_samples = []
        sample_weights = []

        now = datetime.now(timezone.utc)
        for activity, selection in selected:
            activity_effort = self._activity_effort(activity)
            if activity_effort <= 0 or activity.elapsed_time <= 0:
                continue
            elapsed_samples.append(activity.elapsed_time / activity_effort)
            moving_samples.append(activity.moving_time / activity_effort)
            sample_weights.append(
                self._activity_weight(activity, selection, activity_effort, total_effort, now)
            )

        if not elapsed_samples:
            raise ForecastServiceError("Selected activities do not contain enough time data")

        elapsed_pace = self._weighted_median(elapsed_samples, sample_weights)
        moving_pace = self._weighted_median(moving_samples, sample_weights)
        expected_total = max(1, round(elapsed_pace * total_effort))
        moving_total = min(expected_total, max(1, round(moving_pace * total_effort)))
        uncertainty = self._uncertainty(elapsed_samples, sample_weights, elapsed_pace)
        optimistic_total = round(expected_total * (1 - uncertainty * 0.8))
        conservative_total = round(expected_total * (1 + uncertainty))

        checkpoints = self._prepare_checkpoints(route, request.checkpoints)
        results = []
        for checkpoint in checkpoints:
            fraction, elevation = self._checkpoint_fraction(route, checkpoint.distance_km)
            optimistic_seconds = round(optimistic_total * fraction)
            expected_seconds = round(expected_total * fraction)
            conservative_seconds = round(conservative_total * fraction)
            results.append(
                ForecastCheckpointResult(
                    name=checkpoint.name,
                    distance_km=round(checkpoint.distance_km, 2),
                    elevation_m=elevation,
                    optimistic_seconds=optimistic_seconds,
                    expected_seconds=expected_seconds,
                    conservative_seconds=conservative_seconds,
                    optimistic_at=self._eta(request.start_time, optimistic_seconds),
                    expected_at=self._eta(request.start_time, expected_seconds),
                    conservative_at=self._eta(request.start_time, conservative_seconds),
                )
            )

        race_count = sum(selection.is_race for _, selection in selected)
        return ForecastResponse(
            route=self._route_preview(request.route_id, route),
            checkpoints=results,
            moving_time_seconds=moving_total,
            stop_time_seconds=max(0, expected_total - moving_total),
            expected_finish_seconds=expected_total,
            uncertainty_percent=round(uncertainty * 100, 1),
            confidence=self._confidence(len(elapsed_samples), race_count, uncertainty),
            activities_used=len(elapsed_samples),
            races_used=race_count,
            method=(
                "Weighted trail effort: distance, ascent/descent, route similarity, "
                "recency, race priority and late-race fatigue"
            ),
        )

    def _candidate_from_activity(self, activity: Activity) -> ForecastActivityCandidate:
        race_pattern = re.compile(
            r"race|гонк|марафон|ультра|забег", re.IGNORECASE
        )
        return ForecastActivityCandidate(
            id=activity.id,
            source=activity.source or "garmin",
            name=activity.name,
            start_date=activity.start_date,
            sport_type=activity.sport_type,
            distance_km=round(activity.distance / 1000, 2),
            elevation_gain_m=round(activity.total_elevation_gain, 0),
            moving_time=activity.moving_time,
            elapsed_time=activity.elapsed_time,
            suggested_race=activity.workout_type == 1 or bool(race_pattern.search(activity.name)),
        )

    def _route_preview(self, route_id: str, route: ParsedRoute) -> RoutePreview:
        return RoutePreview(
            route_id=route_id,
            name=route.name,
            distance_km=round(route.distance_km, 2),
            elevation_gain_m=round(route.elevation_gain_m, 0),
            elevation_loss_m=round(route.elevation_loss_m, 0),
            checkpoints=route.checkpoints,
        )

    def _enrich_route_points(self, points: List[RoutePoint]) -> None:
        total_distance = 0.0
        ascent = 0.0
        descent = 0.0
        effort = 0.0
        for index in range(1, len(points)):
            previous = points[index - 1]
            point = points[index]
            segment_km = self._haversine_m(
                previous.latitude,
                previous.longitude,
                point.latitude,
                point.longitude,
            ) / 1000
            total_distance += segment_km
            elevation_delta = 0.0
            if previous.elevation_m is not None and point.elevation_m is not None:
                elevation_delta = point.elevation_m - previous.elevation_m
                if elevation_delta > 0:
                    ascent += elevation_delta
                else:
                    descent += abs(elevation_delta)

            segment_effort = segment_km
            if elevation_delta > 0:
                segment_effort += elevation_delta / 100
            elif elevation_delta < 0:
                segment_effort += abs(elevation_delta) / 300
            progress = total_distance / max(total_distance, 0.001)
            # Recalculated below once the route length is known.
            effort += segment_effort * (0.88 + 0.24 * progress**1.5)
            point.distance_km = total_distance
            point.ascent_m = ascent
            point.descent_m = descent
            point.weighted_effort = effort

        route_distance = max(points[-1].distance_km, 0.001)
        effort = 0.0
        points[0].weighted_effort = 0.0
        for index in range(1, len(points)):
            previous = points[index - 1]
            point = points[index]
            segment_km = point.distance_km - previous.distance_km
            ascent_delta = point.ascent_m - previous.ascent_m
            descent_delta = point.descent_m - previous.descent_m
            segment_effort = segment_km + ascent_delta / 100 + descent_delta / 300
            progress = ((previous.distance_km + point.distance_km) / 2) / route_distance
            effort += segment_effort * (0.88 + 0.24 * progress**1.5)
            point.weighted_effort = effort

    def _route_effort(self, route: ParsedRoute) -> float:
        return max(route.points[-1].weighted_effort, 0.001)

    @staticmethod
    def _activity_effort(activity: Activity) -> float:
        distance_km = activity.distance / 1000
        # Historical summaries have ascent but not descent. A loop-like descent is
        # used as a conservative approximation so both sides use the same scale.
        return (
            distance_km
            + activity.total_elevation_gain / 100
            + activity.total_elevation_gain / 300
        )

    @staticmethod
    def _activity_weight(
        activity: Activity,
        selection: HistoricalActivitySelection,
        activity_effort: float,
        route_effort: float,
        now: datetime,
    ) -> float:
        similarity = 1 / (1 + abs(math.log(max(activity_effort, 0.1) / route_effort)))
        activity_date = activity.start_date
        if activity_date.tzinfo is None:
            activity_date = activity_date.replace(tzinfo=timezone.utc)
        age_years = max(0.0, (now - activity_date.astimezone(timezone.utc)).days / 365.25)
        recency = 0.65 + 0.35 * math.exp(-age_years / 3)
        race_priority = 2.5 if selection.is_race else 1.0
        return similarity * recency * race_priority

    @staticmethod
    def _weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
        ordered = sorted(zip(values, weights), key=lambda pair: pair[0])
        midpoint = sum(weights) / 2
        cumulative = 0.0
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= midpoint:
                return value
        return ordered[-1][0]

    def _uncertainty(
        self, values: Sequence[float], weights: Sequence[float], center: float
    ) -> float:
        if len(values) < 3 or center <= 0:
            return 0.12
        relative_errors = [abs(value - center) / center for value in values]
        spread = self._weighted_median(relative_errors, weights) * 1.5
        return min(0.30, max(0.08, spread))

    def _prepare_checkpoints(
        self, route: ParsedRoute, supplied: Iterable[CheckpointInput]
    ) -> List[CheckpointInput]:
        checkpoints = sorted(supplied, key=lambda item: item.distance_km)
        for checkpoint in checkpoints:
            if checkpoint.distance_km > route.distance_km + 0.01:
                raise ForecastServiceError(
                    f"Checkpoint '{checkpoint.name}' is beyond the route distance"
                )
        if not checkpoints or checkpoints[-1].distance_km < route.distance_km - 0.01:
            checkpoints.append(
                CheckpointInput(name="Финиш", distance_km=route.distance_km)
            )
        return checkpoints

    @staticmethod
    def _checkpoint_fraction(
        route: ParsedRoute, distance_km: float
    ) -> tuple[float, Optional[float]]:
        nearest = min(route.points, key=lambda point: abs(point.distance_km - distance_km))
        total_effort = max(route.points[-1].weighted_effort, 0.001)
        return min(1.0, nearest.weighted_effort / total_effort), nearest.elevation_m

    @staticmethod
    def _eta(start_time: Optional[datetime], seconds: int) -> Optional[datetime]:
        return start_time + timedelta(seconds=seconds) if start_time else None

    @staticmethod
    def _confidence(activity_count: int, race_count: int, uncertainty: float) -> str:
        if activity_count >= 5 and race_count >= 2 and uncertainty <= 0.15:
            return "high"
        if activity_count >= 3 and race_count >= 1:
            return "medium"
        return "low"

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_m = 6_371_000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        )
        return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
