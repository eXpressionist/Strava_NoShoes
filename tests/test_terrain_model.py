"""Behavioural regression cases for terrain costs and GPX normalization."""

import math

import pytest

from app.services.terrain_model import (
    RoutePoint,
    enrich_points,
    fatigue_integral,
    pace_factor,
)


def point(metres, elevation):
    return RoutePoint(0.0, math.degrees(metres / 6_371_000), elevation)


def profile(knots, step=25):
    """A piecewise linear height profile with specified distance/elevation knots."""
    points = []
    for (start, low), (end, high) in zip(knots, knots[1:]):
        for distance in range(start, end, step):
            elevation = low + (high - low) * (distance - start) / (end - start)
            points.append(point(distance, elevation))
    points.append(point(*knots[-1]))
    return points


def test_uphill_cost_increases_with_gradient_without_polynomial_extrapolation():
    factors = [pace_factor(g) for g in (0, 0.05, 0.1, 0.2, 0.3, 0.45, 0.6, 1)]
    assert factors[0] == 1
    assert all(a < b for a, b in zip(factors, factors[1:]))
    assert all(math.isfinite(f) for f in factors)


def test_steep_downhill_is_slower_than_flat_and_gentle_downhill():
    assert 0.85 <= pace_factor(-0.05) < 1
    assert pace_factor(-0.30) > pace_factor(-0.20) > 1
    assert pace_factor(-0.60) > pace_factor(-0.45) > pace_factor(-0.30)


def test_same_distance_and_gain_but_concentrated_slopes_cost_more():
    gentle, _ = enrich_points(profile([(0, 0), (5000, 500), (10000, 0)]))
    steep, _ = enrich_points(profile([(0, 0), (1000, 500), (2000, 0), (10000, 0)]))
    assert steep[-1].distance_km == pytest.approx(gentle[-1].distance_km)
    assert steep[-1].ascent_m == pytest.approx(gentle[-1].ascent_m, rel=0.03)
    assert steep[-1].weighted_effort > gentle[-1].weighted_effort * 1.15


def test_recording_density_does_not_change_forecast():
    knots = [(0, 100), (2000, 500), (5000, 100)]
    dense, _ = enrich_points(profile(knots, step=5))
    sparse, _ = enrich_points(profile(knots, step=100))
    assert dense[-1].weighted_effort == pytest.approx(
        sparse[-1].weighted_effort, rel=0.001
    )


def test_isolated_height_spike_and_stationary_samples_do_not_add_climbing():
    clean = profile([(0, 100), (1000, 100)])
    noisy = profile([(0, 100), (1000, 100)])
    noisy[20].elevation_m = 140
    noisy.insert(10, point(225, 500))
    actual, _ = enrich_points(noisy)
    expected, _ = enrich_points(clean)
    assert actual[-1].ascent_m == pytest.approx(0)
    assert actual[-1].weighted_effort == pytest.approx(expected[-1].weighted_effort)


def test_short_missing_elevation_gap_is_interpolated():
    points = profile([(0, 0), (1000, 100)])
    points[10].elevation_m = None
    result, warnings = enrich_points(points)
    assert warnings
    assert result[-1].ascent_m == pytest.approx(100)


@pytest.mark.parametrize(
    "points",
    [
        [point(0, None), point(1000, None)],
        [point(0, 0), point(500, None), point(1000, 100)],
        [point(0, 0), point(100, float("nan"))],
        [point(0, 0), point(0, 100)],
    ],
)
def test_invalid_or_missing_profile_is_not_treated_as_flat(points):
    with pytest.raises(ValueError):
        enrich_points(points)


def test_fatigue_never_discounts_effort_and_longer_races_slow_down():
    assert fatigue_integral(10) >= 10
    assert fatigue_integral(100) / 100 > fatigue_integral(20) / 20


def test_cumulative_time_is_strictly_increasing_on_mixed_terrain():
    points, _ = enrich_points(profile([(0, 0), (500, 150), (1000, 0)]))
    assert all(
        a.weighted_effort < b.weighted_effort for a, b in zip(points, points[1:])
    )
