from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import ActivityDB, Base, GearDB
from app.services.backup_service import StravaBackupScheduler
from app.services.garmin_service import GarminService
from scripts import backup_garmin, backup_strava


def test_backup_cutoff_is_inclusive(monkeypatch):
    monkeypatch.setattr(settings, "migration_cutoff", "2026-10-15")
    monkeypatch.setattr(settings, "strava_backup_enabled", True)
    monkeypatch.setattr(settings, "garmin_sync_enabled", True)
    scheduler = StravaBackupScheduler()

    assert scheduler.should_run(date(2026, 10, 15)) is True
    assert scheduler.source_for_date(date(2026, 10, 15)) == "strava"
    assert scheduler.should_run(date(2026, 10, 16)) is True
    assert scheduler.source_for_date(date(2026, 10, 16)) == "garmin"


def test_garmin_sync_can_be_disabled_after_cutoff(monkeypatch):
    monkeypatch.setattr(settings, "migration_cutoff", "2026-10-15")
    monkeypatch.setattr(settings, "garmin_sync_enabled", False)

    assert StravaBackupScheduler().should_run(date(2026, 10, 16)) is False


@pytest.mark.asyncio
async def test_activity_backup_is_idempotent_and_refreshes_existing_rows(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backup.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(backup_strava, "async_session", session_factory)

    activity = {
        "id": 101,
        "name": "Original name",
        "sport_type": "Run",
        "type": "Run",
        "distance": 10_000,
        "moving_time": 3_600,
        "elapsed_time": 3_700,
        "total_elevation_gain": 100,
        "start_date": "2026-08-01T06:00:00Z",
        "start_date_local": "2026-08-01T10:00:00Z",
        "start_latlng": [41.7, 44.8],
        "gear_id": "shoe-1",
    }

    class FakeStrava:
        async def _make_request(self, method, endpoint, params=None):
            return [activity]

    backup = backup_strava.StravaBackup(streams_limit=0)
    backup.strava = FakeStrava()
    await backup.backup_activities()

    activity["name"] = "Updated name"
    activity["gear_id"] = "shoe-2"
    await backup.backup_activities()

    async with session_factory() as session:
        rows = (await session.execute(select(ActivityDB))).scalars().all()

    assert len(rows) == 1
    assert rows[0].name == "Updated name"
    assert rows[0].gear_id == "shoe-2"
    await engine.dispose()


@pytest.mark.asyncio
async def test_garmin_sync_is_idempotent_and_refreshes_gear(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'garmin.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(backup_garmin, "async_session", session_factory)

    service = GarminService()
    activity = service._garmin_activity_to_model(
        {
            "activityId": 202,
            "activityName": "Morning Run",
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-10-16 07:00:00",
            "startTimeGMT": "2026-10-16 03:00:00",
            "distance": 10_000,
            "duration": 3_600,
            "movingDuration": 3_500,
        }
    )
    activity.gear_id = "shoe-1"
    activity.gear_name = "Daily shoes"

    backup = backup_garmin.GarminBackup(garmin=service)
    await backup._upsert_activities([activity])
    activity.name = "Updated Morning Run"
    activity.gear_name = "Renamed shoes"
    await backup._upsert_activities([activity])

    async with session_factory() as session:
        activities = (await session.execute(select(ActivityDB))).scalars().all()
        gear = (await session.execute(select(GearDB))).scalars().all()

    assert len(activities) == 1
    assert activities[0].source == "garmin"
    assert activities[0].name == "Updated Morning Run"
    assert len(gear) == 1
    assert gear[0].name == "Renamed shoes"
    assert backup.stats["activities_saved"] == 1
    assert backup.stats["activities_updated"] == 1
    await engine.dispose()
