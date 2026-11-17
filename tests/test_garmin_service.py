from datetime import datetime

import pytest

from app.services.garmin_service import GarminService


def garmin_activity(activity_id: int = 123) -> dict:
    return {
        "activityId": activity_id,
        "activityName": "Morning Run",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-10-16 07:00:00",
        "startTimeGMT": "2026-10-16 03:00:00",
        "distance": 10_000,
        "duration": 3_600,
        "movingDuration": 3_500,
        "averageSpeed": 2.8,
    }


@pytest.mark.asyncio
async def test_personal_api_does_not_guess_missing_gear(monkeypatch):
    service = GarminService()
    activity = service._garmin_activity_to_model(garmin_activity())

    async def get_activities(*args, **kwargs):
        return [activity]

    monkeypatch.setattr(service, "get_activities", get_activities)
    assert activity.gear_status_known is False
    assert activity.gear_id is None
    assert await service.get_activities_without_gear() == []
    assert await service.get_athlete_gear() == []


def test_garmin_detail_response_uses_summary_dto():
    service = GarminService()
    activity = service._garmin_activity_to_model(
        {
            "activityId": 456,
            "activityName": "Detailed Run",
            "activityTypeDTO": {"typeKey": "trail_running"},
            "metadataDTO": {"isManualActivity": False, "isPrivate": True},
            "summaryDTO": {
                "startTimeLocal": "2026-10-17 08:00:00",
                "startTimeGMT": "2026-10-17 04:00:00",
                "distance": 20_000,
                "duration": 7_200,
                "movingDuration": 7_000,
                "elevationGain": 800,
            },
        }
    )

    assert activity.sport_type == "TrailRun"
    assert activity.distance == 20_000
    assert activity.start_date == datetime(2026, 10, 17, 4, 0)
    assert activity.private is True


def test_official_activity_details_preserve_metadata_and_summary():
    activity = GarminService()._garmin_activity_to_model(
        {
            "activityId": 789,
            "activityName": "Private run",
            "activityTypeDTO": {"typeKey": "running"},
            "metadataDTO": {"manualActivity": True},
            "accessControlRuleDTO": {"typeKey": "private"},
            "timeZoneUnitDTO": {"timeZone": "Europe/Moscow"},
            "summaryDTO": {
                "startTimeLocal": "2026-11-17T09:00:00.000",
                "startTimeGMT": "2026-11-17T06:00:00.000",
                "duration": 3000,
                "elapsedDuration": 3600,
                "averageRunCadence": 175,
                "maxElevation": 200,
                "minElevation": 100,
            },
        }
    )
    assert activity.manual and activity.private
    assert activity.visibility == "only_me"
    assert activity.timezone == "Europe/Moscow"
    assert activity.utc_offset == 10800
    assert activity.elapsed_time == 3600
    assert activity.average_cadence == 175
    assert activity.elev_high == 200
    assert activity.elev_low == 100
