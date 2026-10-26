"""Health collection keeps raw series, resumes history and isolates failures."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, settings
from app.models.database import Base, GarminHealthDB
from app.services import garmin_health_service as health

TODAY = date(2026, 10, 26)


@pytest_asyncio.fixture
async def health_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def init_test_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(health, "async_session", sessions)
    monkeypatch.setattr(health, "init_db", init_test_db)
    monkeypatch.setattr(health, "HEALTH_METRICS", {"hrv": "get_hrv_data"})
    monkeypatch.setattr(settings, "garmin_health_history_days", 5)
    monkeypatch.setattr(settings, "garmin_health_lookback_days", 2)
    monkeypatch.setattr(settings, "garmin_health_backfill_days_per_run", 1)
    monkeypatch.setattr(settings, "garmin_health_start_date", "")
    monkeypatch.setattr(settings, "garmin_health_request_delay_seconds", 0)
    await init_test_db()
    yield sessions
    await engine.dispose()


def collector(**methods):
    garmin = SimpleNamespace(
        _ensure_connected=AsyncMock(), client=SimpleNamespace(**methods)
    )
    return health.GarminHealthSync(garmin=garmin)


@pytest.mark.asyncio
async def test_raw_series_upsert_and_resumable_backfill(health_db):
    payload = {"hrvValues": [[123456789, 62.5], [123456790, None]], "units": "ms"}
    method = Mock(return_value=payload)
    sync = collector(get_hrv_data=method)

    first = await sync.run(TODAY)
    assert first == {"records_saved": 3, "empty_responses": 0, "errors": []}
    assert [call.args[0] for call in method.call_args_list] == [
        "2026-10-26",
        "2026-10-25",
        "2026-10-22",
    ]
    method.reset_mock()
    method.return_value = {"hrvValues": [[123456789, 70]]}
    await sync.run(TODAY)
    assert [call.args[0] for call in method.call_args_list] == [
        "2026-10-26",
        "2026-10-25",
        "2026-10-23",
    ]
    # A newly constructed collector resumes from persisted responses.
    await collector(get_hrv_data=Mock(return_value=payload)).run(TODAY)
    async with health_db() as session:
        rows = (await session.scalars(select(GarminHealthDB))).all()
        status = await health.GarminHealthScheduler().status(session)
    assert len(rows) == 5
    assert all(
        row.payload == payload
        for row in rows
        if row.calendar_date != date(2026, 10, 23)
    )
    assert status["metrics"]["hrv"]["records"] == 5
    assert status["metrics"]["hrv"]["days_with_data"] == 5
    assert status["metrics"]["hrv"]["first_date"] == date(2026, 10, 22)
    assert "hrvValues" not in str(status)


@pytest.mark.asyncio
async def test_empty_data_is_saved_without_turning_it_into_zero(health_db):
    method = Mock(return_value=None)
    result = await collector(get_hrv_data=method).run(TODAY)
    assert result["empty_responses"] == 3
    async with health_db() as session:
        rows = (await session.scalars(select(GarminHealthDB))).all()
    assert all(row.payload is None and row.has_data is False for row in rows)
    # An empty historical response is complete; the next run advances history.
    assert await collector()._dates("hrv", TODAY) == [
        TODAY,
        date(2026, 10, 25),
        date(2026, 10, 23),
    ]


@pytest.mark.asyncio
async def test_sensor_failure_preserves_history_and_other_metrics_progress(
    health_db, monkeypatch
):
    await collector()._save("hrv", TODAY, {"value": 55})
    monkeypatch.setattr(
        health, "HEALTH_METRICS", {"hrv": "get_hrv_data", "sleep": "get_sleep_data"}
    )
    failure = GarminConnectConnectionError("private response body")
    failure.response = SimpleNamespace(status_code=403)
    sync = collector(
        get_hrv_data=Mock(side_effect=failure),
        get_sleep_data=Mock(return_value={"sleep": [1, 2]}),
    )
    result = await sync.run(TODAY)
    assert len(result["errors"]) == 1
    assert "private response body" not in str(result)
    assert result["records_saved"] == 3
    async with health_db() as session:
        row = await session.scalar(
            select(GarminHealthDB).where(GarminHealthDB.metric == "hrv")
        )
    assert row.payload == {"value": 55}


@pytest.mark.asyncio
async def test_missing_day_does_not_block_older_data(health_db):
    missing = GarminConnectConnectionError("not found")
    missing.response = SimpleNamespace(status_code=404)
    method = Mock(side_effect=[missing, {"value": 55}, {"value": 50}])
    result = await collector(get_hrv_data=method).run(TODAY)
    assert result == {"records_saved": 3, "empty_responses": 1, "errors": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [GarminConnectAuthenticationError, GarminConnectTooManyRequestsError]
)
async def test_auth_and_rate_limits_stop_requests_but_keep_progress(
    health_db, monkeypatch, failure
):
    monkeypatch.setattr(
        health, "HEALTH_METRICS", {"hrv": "get_hrv_data", "sleep": "get_sleep_data"}
    )
    sleep = Mock()
    sync = collector(
        get_hrv_data=Mock(side_effect=[{"value": 55}, failure("sensitive")]),
        get_sleep_data=sleep,
    )
    result = await sync.run(TODAY)
    assert result["records_saved"] == 1
    assert result["errors"][0]["error"] == failure.__name__
    sleep.assert_not_called()
    async with health_db() as session:
        assert await session.scalar(select(GarminHealthDB.payload)) == {"value": 55}


@pytest.mark.asyncio
async def test_fixed_history_start_and_future_start(health_db, monkeypatch):
    sync = collector()
    monkeypatch.setattr(settings, "garmin_health_start_date", "2020-01-01")
    assert (await sync._dates("hrv", TODAY))[-1] == date(2020, 1, 1)
    monkeypatch.setattr(settings, "garmin_health_start_date", "2027-01-01")
    assert await sync._dates("hrv", TODAY) == []


@pytest.mark.asyncio
async def test_scheduler_runs_separately_and_skips_overlapping_requests(monkeypatch):
    monkeypatch.setattr(health.GarminHealthScheduler, "should_run", lambda self: True)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def run_sync(self):
        started.set()
        await finish.wait()
        return {"records_saved": 1, "errors": []}

    monkeypatch.setattr(health.GarminHealthSync, "run", run_sync)
    scheduler = health.GarminHealthScheduler()
    task = asyncio.create_task(scheduler.run_sync())
    await started.wait()
    assert await scheduler.run_sync() is None
    finish.set()
    assert await task == {"records_saved": 1, "errors": []}
    assert scheduler.last_error is None


@pytest.mark.asyncio
async def test_scheduler_starts_in_background_and_cancels_on_shutdown(
    health_db, monkeypatch
):
    monkeypatch.setattr(health.GarminHealthScheduler, "should_run", lambda self: True)
    monkeypatch.setattr(
        health.GarminHealthScheduler, "is_configured", lambda self: True
    )
    started = asyncio.Event()

    async def run_sync(self):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(health.GarminHealthSync, "run", run_sync)
    scheduler = health.GarminHealthScheduler()
    await scheduler.start()
    await started.wait()
    assert len(scheduler.scheduler.get_jobs()) == 1
    await scheduler.shutdown()
    assert scheduler._startup_task.cancelled()
    assert not scheduler._lock.locked()


def test_secondary_disabled_and_unconfigured_instances_do_not_collect(monkeypatch):
    monkeypatch.setattr(settings, "garmin_health_sync_enabled", True)
    monkeypatch.setattr(settings, "migration_enabled", True)
    monkeypatch.setattr(settings, "migration_cutoff", "2000-01-01")
    monkeypatch.setattr(settings, "garmin_email", "test@example.com")
    monkeypatch.setattr(settings, "garmin_password", "test")
    assert health.GarminHealthScheduler.should_run()
    monkeypatch.setattr(settings, "garmin_sync_enabled", False)
    assert health.GarminHealthScheduler.should_run()
    monkeypatch.setattr(settings, "migration_enabled", False)
    assert not health.GarminHealthScheduler.should_run()
    monkeypatch.setattr(settings, "migration_enabled", True)
    monkeypatch.setattr(settings, "garmin_health_sync_enabled", False)
    assert not health.GarminHealthScheduler.should_run()
    monkeypatch.setattr(settings, "garmin_health_sync_enabled", True)
    monkeypatch.setattr(settings, "garmin_password", "")
    assert not health.GarminHealthScheduler.should_run()


@pytest.mark.asyncio
async def test_collection_uses_health_timezone_not_server_date(health_db, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, 22, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(health, "datetime", FixedDatetime)
    monkeypatch.setattr(settings, "garmin_health_timezone", "Europe/Moscow")
    method = Mock(return_value={})
    await collector(get_hrv_data=method).run()
    assert method.call_args_list[0].args == ("2026-10-26",)


@pytest.mark.parametrize(
    "values", [dict(garmin_health_start_date="bad"), dict(garmin_health_timezone="bad")]
)
def test_invalid_health_dates_and_timezones_fail_validation(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_metric_methods_exist_in_installed_garmin_library():
    for method in health.HEALTH_METRICS.values():
        assert callable(getattr(Garmin, method))
