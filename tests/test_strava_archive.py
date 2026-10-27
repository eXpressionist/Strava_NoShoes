"""Exercise the actual SQLite source with all Strava network access disabled."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.models.database import ActivityDB, Base, GearDB
from app.services import strava_archive
from app.services.gear_migration_service import GearMigration
from app.services.strava_archive import StravaArchive
from app.services.strava_service import StravaService
from tests.test_gear_migration import SHOE, FakeGarmin


@pytest_asyncio.fixture
async def archive_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(strava_archive, "async_session", sessions)
    monkeypatch.setattr(settings, "strava_access_token", "")
    monkeypatch.setattr(settings, "strava_refresh_token", "")
    monkeypatch.setattr(
        StravaService,
        "_make_request",
        AsyncMock(side_effect=AssertionError("Strava API must never be used")),
    )
    async with sessions() as session:
        session.add(
            ActivityDB(
                source="strava",
                source_id="1",
                name="Archived run",
                sport_type="Run",
                distance=10000,
                elapsed_time=3600,
                start_date=datetime(2026, 10, 20, 6),
                start_date_local=datetime(2026, 10, 20, 9),
                gear_id="g1",
                gear_name="Daily shoes",
                raw_data=json.dumps(
                    {"athlete": {"id": 42}, "gear_id": "outdated-value"}
                ),
            )
        )
        session.add(
            GearDB(
                source="strava",
                source_id="g1",
                name="Daily shoes",
                distance=650000,
                brand="Nike",
                model="Pegasus",
            )
        )
        # Same original ID from Garmin must never contaminate the Strava source.
        session.add(
            ActivityDB(
                source="garmin",
                source_id="1",
                name="Garmin run",
                sport_type="Run",
                distance=999,
                elapsed_time=1,
                start_date=datetime(2026, 10, 21, 6),
                start_date_local=datetime(2026, 10, 21, 9),
                gear_id="unrelated",
                raw_data=json.dumps({"athlete": {"id": 999}}),
            )
        )
        await session.commit()
    yield sessions
    await engine.dispose()


@pytest.mark.asyncio
async def test_default_migration_uses_sqlite_for_both_plan_and_apply(archive_db):
    garmin = SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock())
    sync = GearMigration(garmin=garmin, delay=0)
    plan = await sync.build_plan(date(2025, 10, 27), date(2026, 10, 27))
    assert plan["archive"]["source"] == "sqlite"
    assert plan["archive"]["latest_activity_utc"] == "2026-10-20T06:00:00"
    assert plan["strava_athlete_id"] == 42
    assert plan["strava_activities"] == 1
    assert plan["gear"]["g1"]["brand"] == "Nike"
    assert plan["summary"] == {"link": 1}
    assert (await sync.apply(plan, lambda event: None))["linked"] == 1
    assert (
        await sync.apply(
            await sync.build_plan(date(2025, 10, 27), date(2026, 10, 27)),
            lambda event: None,
        )
    )["already_linked"] == 1
    StravaService._make_request.assert_not_called()
    async with archive_db() as session:
        assert await session.scalar(select(func.count(ActivityDB.id))) == 2
        assert await session.scalar(select(func.count(GearDB.id))) == 1


@pytest.mark.asyncio
async def test_archive_bounds_and_gear_metadata_fallback(archive_db):
    source = StravaArchive()
    assert await source.activities(
        datetime(2026, 10, 20, 6, tzinfo=timezone.utc),
        datetime(2026, 10, 20, 6, 0, 1, tzinfo=timezone.utc),
    )
    assert (
        await source.activities(
            datetime(2026, 10, 20, 5, tzinfo=timezone.utc),
            datetime(2026, 10, 20, 6, tzinfo=timezone.utc),
        )
        == []
    )
    async with archive_db() as session:
        gear = await session.scalar(select(GearDB))
        await session.delete(gear)
        await session.commit()
    assert await source.gear("g1") == {"id": "g1", "name": "Daily shoes"}


@pytest.mark.asyncio
async def test_missing_athlete_metadata_still_migrates_and_can_resume(archive_db):
    async with archive_db() as session:
        activity = await session.scalar(
            select(ActivityDB).where(ActivityDB.source == "strava")
        )
        activity.raw_data = "invalid legacy JSON"
        await session.commit()
    sync = GearMigration(
        garmin=SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock()),
        delay=0,
    )
    plan = await sync.build_plan(date(2025, 10, 27), date(2026, 10, 27))
    assert plan["strava_athlete_id"] is None
    assert plan["gear"]["g1"]["notes"] == "[noshoes:strava:archive:g1]"
    assert (await sync.apply(plan, lambda event: None))["linked"] == 1


@pytest.mark.asyncio
async def test_empty_window_fails_before_contacting_garmin(archive_db):
    garmin = SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock())
    sync = GearMigration(garmin=garmin, delay=0)
    with pytest.raises(ValueError, match="No archived Strava activities"):
        await sync.build_plan(date(2020, 1, 1), date(2020, 12, 31))
    garmin._ensure_connected.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_does_not_require_strava_tokens(archive_db, monkeypatch, tmp_path):
    from scripts import migrate_gear

    sync = GearMigration(
        garmin=SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock()),
        delay=0,
    )
    monkeypatch.setattr(settings, "garmin_email", "test@example.com")
    monkeypatch.setattr(settings, "garmin_password", "test")
    monkeypatch.setattr(migrate_gear, "GearMigration", lambda **kwargs: sync)
    args = SimpleNamespace(
        timezone="Europe/Moscow",
        from_date=date(2025, 10, 27),
        to_date=date(2026, 10, 27),
        request_delay=0,
        gear_map=None,
        report=tmp_path / "report.json",
        journal=tmp_path / "journal.jsonl",
        apply=True,
        replace_existing=False,
    )
    assert await migrate_gear.run(args) == 0
    report = json.loads(args.report.read_text(encoding="utf-8"))
    assert report["archive"]["source"] == "sqlite"
    assert report["result"]["linked"] == 1
    StravaService._make_request.assert_not_called()


@pytest.mark.asyncio
async def test_empty_archive_stops_before_garmin_calls(archive_db):
    async with archive_db() as session:
        activity = await session.scalar(
            select(ActivityDB).where(ActivityDB.source == "strava")
        )
        await session.delete(activity)
        await session.commit()
    garmin = SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock())
    with pytest.raises(ValueError, match="no Strava activities"):
        await GearMigration(garmin=garmin, delay=0).build_plan(
            date(2025, 10, 27), date(2026, 10, 27)
        )
    garmin._ensure_connected.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_gear_name_requires_mapping_before_writes(archive_db):
    async with archive_db() as session:
        row = await session.scalar(select(GearDB))
        await session.delete(row)
        activity = await session.scalar(
            select(ActivityDB).where(ActivityDB.source == "strava")
        )
        activity.gear_name = None
        await session.commit()
    client = FakeGarmin()
    client.gear = [{"uuid": SHOE, "gearType": "SHOES", "name": "Garmin shoes"}]
    sync = GearMigration(
        garmin=SimpleNamespace(client=client, _ensure_connected=AsyncMock()), delay=0
    )
    plan = await sync.build_plan(date(2025, 10, 27), date(2026, 10, 27))
    assert plan["summary"] == {"missing_gear_metadata": 1}
    assert (await sync.apply(plan, lambda event: None))["skipped"] == 1
    assert client.writes == []
    plan = await sync.build_plan(
        date(2025, 10, 27), date(2026, 10, 27), mapping={"g1": SHOE}
    )
    assert (await sync.apply(plan, lambda event: None))["linked"] == 1


@pytest.mark.asyncio
async def test_mixed_archive_owners_stop_migration(archive_db):
    async with archive_db() as session:
        session.add(
            ActivityDB(
                source="strava",
                source_id="2",
                name="Other athlete",
                sport_type="Run",
                start_date=datetime(2026, 10, 21, 6),
                start_date_local=datetime(2026, 10, 21, 9),
                raw_data=json.dumps({"athlete": {"id": 999}}),
            )
        )
        await session.commit()
    garmin = SimpleNamespace(client=FakeGarmin(), _ensure_connected=AsyncMock())
    with pytest.raises(ValueError, match="multiple athletes"):
        await GearMigration(garmin=garmin, delay=0).build_plan(
            date(2025, 10, 27), date(2026, 10, 27)
        )
    garmin._ensure_connected.assert_not_awaited()
