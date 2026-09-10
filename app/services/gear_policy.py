"""Policy for deciding which activity types require gear reminders."""

from app.models.strava import Activity

GEAR_EXEMPT_SPORT_TYPES = frozenset({"WeightTraining"})


def requires_gear(activity: Activity) -> bool:
    """Return whether missing gear should be reported for an activity."""
    return activity.sport_type not in GEAR_EXEMPT_SPORT_TYPES
