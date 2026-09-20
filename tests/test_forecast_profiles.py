"""Historical profile retrieval must preserve source identity and sample order."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.database import ActivityDB, ActivityStreamDB, Base
from app.services import unified_service
from app.services.strava_service import StravaAPIError
from app.services.unified_service import UnifiedActivityService


@pytest.mark.asyncio
async def test_live_strava_profile_uses_streams_without_exporting_files(monkeypatch):
    service = UnifiedActivityService()
    monkeypatch.setattr(service, "strava_is_live", lambda: True)
    streams = AsyncMock(
        return_value={
            "latlng": {"data": [[1, 2], [3, 4]]},
            "altitude": {"data": [50, 100]},
        }
    )
    monkeypatch.setattr(service.strava, "get_activity_streams", streams)
    result = await service.get_activity_profile(42, "strava")
    assert result == [(1, 2, 50), (3, 4, 100)]
    streams.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_stored_profiles_use_source_and_order_and_survive_api_failure(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'profiles.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            for source, altitude in [("strava", 100), ("garmin", 200)]:
                activity = ActivityDB(
                    source=source,
                    source_id="42",
                    name=source,
                    sport_type="TrailRun",
                    start_date=datetime(2025, 1, 1),
                    start_date_local=datetime(2025, 1, 1),
                )
                session.add(activity)
                await session.flush()
                for index in (1, 0):
                    session.add(
                        ActivityStreamDB(
                            activity_id=activity.id,
                            point_index=index,
                            latitude=55 + index * 0.001,
                            longitude=37,
                            altitude=altitude + index,
                        )
                    )
            await session.commit()

        monkeypatch.setattr(unified_service, "async_session", sessions)
        service = UnifiedActivityService()
        service._db_initialized = True
        monkeypatch.setattr(service, "strava_is_live", lambda: True)
        live = AsyncMock(side_effect=StravaAPIError("unavailable"))
        monkeypatch.setattr(service.strava, "get_activity_streams", live)

        assert await service.get_activity_profile(42, "strava") == [
            (55, 37, 100),
            (55.001, 37, 101),
        ]
        assert await service.get_activity_profile(42, "garmin") == [
            (55, 37, 200),
            (55.001, 37, 201),
        ]
        live.assert_awaited_once_with(42)
        assert await service.get_activity_profile(999, "garmin") == []
    finally:
        await engine.dispose()
