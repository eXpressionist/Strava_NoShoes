"""Distance-based trail effort, shared by planned and historical routes.

Minetti et al. (2002), doi:10.1152/japplphysiol.01177.2001, describes
energy cost, not achievable downhill speed. The downhill floor and fatigue
below are explicit trail heuristics; they require validation on real races.
"""

import math
from bisect import bisect_right
from dataclasses import dataclass, replace
from statistics import median
from typing import Optional, Sequence

SAMPLE_DISTANCE_M = 25.0
MAX_ELEVATION_GAP_M = 100.0


@dataclass
class RoutePoint:
    latitude: float
    longitude: float
    elevation_m: Optional[float]
    distance_km: float = 0.0
    ascent_m: float = 0.0
    descent_m: float = 0.0
    weighted_effort: float = 0.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = math.sin(math.radians(lat2 - lat1) / 2) ** 2 + (
        math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6_371_000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def pace_factor(grade: float) -> float:
    """Seconds per surface kilometre relative to flat; grade is rise/run."""
    g = max(-0.45, min(0.45, grade))
    cost = (
        155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6
    ) / 3.6
    if grade >= 0:
        # Do not extrapolate a fifth-degree polynomial beyond its study range.
        return cost + 12.0 * max(0.0, grade - 0.45)
    downhill = abs(grade)
    # At most 15% less time on a gentle descent. Braking/control progressively
    # remove that benefit after -5%; steep descents become slower than flat.
    floor = 1.0 - 3.0 * min(downhill, 0.05)
    return max(cost, floor + 18.0 * max(0.0, downhill - 0.05) ** 2)


def fatigue_integral(effort: float) -> float:
    """Integrate 1 + 0.004 * accumulated equivalent km (never a speed bonus)."""
    return effort + 0.002 * effort**2


def summary_effort(distance_km: float, ascent_m: float) -> float:
    """Fallback when slopes are unknown: distance + ascent only, no fake descent.

    Retain the familiar 100 m ascent / equivalent km approximation, but never
    claim that summary data reconstruct a gradient distribution.
    """
    return fatigue_integral(distance_km + ascent_m / 100.0)


def enrich_points(raw: Sequence[RoutePoint]) -> tuple[list[RoutePoint], list[str]]:
    """Validate, resample at 25 m, remove isolated height spikes, integrate cost."""
    points: list[RoutePoint] = []
    distances: list[float] = []
    warnings: list[str] = []
    distance = 0.0
    for point in raw:
        if (
            not math.isfinite(point.latitude)
            or abs(point.latitude) > 90
            or not math.isfinite(point.longitude)
            or abs(point.longitude) > 180
            or (point.elevation_m is not None and not math.isfinite(point.elevation_m))
        ):
            raise ValueError("GPX contains invalid coordinates or elevations")
        if points:
            step = haversine_m(
                points[-1].latitude,
                points[-1].longitude,
                point.latitude,
                point.longitude,
            )
            if step < 0.01:
                # Stationary GPS samples must not generate vertical effort.
                continue
            distance += step
        points.append(replace(point))
        distances.append(distance)
    if len(points) < 2 or distance < 1:
        raise ValueError("GPX must contain at least one metre of track")

    known = [i for i, p in enumerate(points) if p.elevation_m is not None]
    if not known or known[0] != 0 or known[-1] != len(points) - 1:
        raise ValueError("GPX needs elevation data, including start and finish")
    for left, right in zip(known, known[1:]):
        if right == left + 1:
            continue
        if distances[right] - distances[left] > MAX_ELEVATION_GAP_M:
            raise ValueError("GPX has an elevation data gap longer than 100 m")
        low, high = points[left].elevation_m, points[right].elevation_m
        assert low is not None and high is not None
        for i in range(left + 1, right):
            fraction = (distances[i] - distances[left]) / (
                distances[right] - distances[left]
            )
            points[i].elevation_m = low + (high - low) * fraction
    if len(known) != len(points):
        warnings.append("Короткие пропуски высот GPX восстановлены интерполяцией.")
    if max(b - a for a, b in zip(distances, distances[1:])) > 200:
        warnings.append(
            "В GPX есть интервалы более 200 м: локальная крутизна неизвестна."
        )

    # A distance grid avoids dependence on recording frequency or stationary time.
    grid = [
        i * SAMPLE_DISTANCE_M for i in range(math.ceil(distance / SAMPLE_DISTANCE_M))
    ]
    grid.append(distance)
    sampled = []
    for target in grid:
        right = min(bisect_right(distances, target), len(points) - 1)
        left = max(0, right - 1)
        fraction = (target - distances[left]) / (distances[right] - distances[left])
        a, b = points[left], points[right]
        assert a.elevation_m is not None and b.elevation_m is not None
        sampled.append(
            RoutePoint(
                a.latitude + (b.latitude - a.latitude) * fraction,
                a.longitude + (b.longitude - a.longitude) * fraction,
                a.elevation_m + (b.elevation_m - a.elevation_m) * fraction,
                distance_km=target / 1000,
            )
        )
    heights: list[float] = []
    for point in sampled:
        assert point.elevation_m is not None
        heights.append(point.elevation_m)
    for i in range(1, len(sampled) - 1):
        sampled[i].elevation_m = median(heights[i - 1 : i + 2])

    effort = 0.0
    for previous, point in zip(sampled, sampled[1:]):
        assert previous.elevation_m is not None and point.elevation_m is not None
        run = (point.distance_km - previous.distance_km) * 1000
        rise = point.elevation_m - previous.elevation_m
        grade = rise / run
        effort += math.hypot(run, rise) / 1000 * pace_factor(grade)
        point.ascent_m = previous.ascent_m + max(0.0, rise)
        point.descent_m = previous.descent_m + max(0.0, -rise)
        point.weighted_effort = fatigue_integral(effort)
    return sampled, warnings
