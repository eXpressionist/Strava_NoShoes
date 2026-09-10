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
async def test_garmin_empty_activity_gear_means_no_gear():
    class Client:
        def get_activity_gear(self, activity_id):
            return []

    service = GarminService()
    service.client = Client()
    activity = service._garmin_activity_to_model(garmin_activity())

    await service._populate_gear_names([activity])

    assert activity.gear_id is None
    assert activity.gear_name is None


@pytest.mark.asyncio
async def test_garmin_activity_specific_gear_is_attached():
    class Client:
        def get_activity_gear(self, activity_id):
            return [
                {
                    "uuid": "shoe-uuid",
                    "customMakeModel": "Trail shoes",
                    "isActive": True,
                }
            ]

    service = GarminService()
    service.client = Client()
    activity = service._garmin_activity_to_model(garmin_activity())

    await service._populate_gear_names([activity])

    assert activity.gear_id == "shoe-uuid"
    assert activity.gear_name == "Trail shoes"


@pytest.mark.asyncio
async def test_strength_training_does_not_request_or_require_gear(monkeypatch):
    class Client:
        def get_activity_gear(self, activity_id):
            raise AssertionError("Gear must not be queried for strength training")

    data = garmin_activity()
    data["activityType"] = {"typeKey": "strength_training"}
    service = GarminService()
    service.client = Client()
    activity = service._garmin_activity_to_model(data)

    await service._populate_gear_names([activity])

    async def get_activities(*args, **kwargs):
        return [activity]

    monkeypatch.setattr(service, "get_activities", get_activities)

    assert activity.sport_type == "WeightTraining"
    assert activity.gear_id is None
    assert await service.get_activities_without_gear() == []


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
