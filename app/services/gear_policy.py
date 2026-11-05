"""Policy for deciding which activity types require gear reminders."""

from app.models.strava import Activity

GEAR_EXEMPT_SPORT_TYPES = frozenset({"WeightTraining"})
STRAVA_GEAR_EXEMPT_SPORT_TYPES = GEAR_EXEMPT_SPORT_TYPES | {
    "Crossfit",
    "HighIntensityIntervalTraining",
    "PhysicalTherapy",
    "Pilates",
    "Workout",
    "Yoga",
}


def requires_gear(activity: Activity) -> bool:
    """Return whether missing gear should be reported for an activity."""
    exempt_sports = (
        STRAVA_GEAR_EXEMPT_SPORT_TYPES
        if activity.source == "strava"
        else GEAR_EXEMPT_SPORT_TYPES
    )
    return activity.sport_type not in exempt_sports
