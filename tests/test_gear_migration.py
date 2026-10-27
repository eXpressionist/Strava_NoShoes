"""Migration never guesses ambiguous activities or creates duplicate gear."""

import copy
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.gear_migration_service import (
    GearMigration,
    MigrationActivity,
    gear_uuid,
    marker,
    match_activities,
)

FIRST, LAST = date(2025, 10, 27), date(2026, 10, 27)
SHOE = "11111111-1111-1111-1111-111111111111"
OLD_SHOE = "22222222-2222-2222-2222-222222222222"
COMPONENT = "33333333-3333-3333-3333-333333333333"


def strava_activity(activity_id=1, **values):
    return {
        "id": activity_id,
        "name": "Morning run",
        "start_date": "2026-10-20T06:00:00Z",
        "sport_type": "Run",
        "distance": 10000,
        "elapsed_time": 3600,
        "gear_id": "g1",
        **values,
    }


def garmin_activity(activity_id=101, **values):
    return {
        "activityId": activity_id,
        "activityName": "Morning run",
        "startTimeGMT": "2026-10-20 06:00:00",
        "activityType": {"typeKey": "running"},
        "distance": 10001,
        "duration": 3500,
        "elapsedDuration": 3600,
        **values,
    }


class FakeGarmin:
    def __init__(self):
        self.activities = [garmin_activity()]
        self.gear = []
        self.links = {101: []}
        self.writes = []
        self.fail_create_once = False
        self.fail_link_once = False

    def get_activities_by_date(self, *args):
        return copy.deepcopy(self.activities)

    def get_activities(self, start=0, limit=100):
        return copy.deepcopy(self.activities[start : start + limit])

    def get_gear_list(self, start=0, limit=100, **kwargs):
        return copy.deepcopy(self.gear[start : start + limit])

    def get_activity_gear_v2(self, activity_id):
        return copy.deepcopy(self.links.get(activity_id, []))

    def create_gear(self, **values):
        self.writes.append(("create", values))
        self.gear.append(
            {
                "uuid": SHOE,
                "gearType": values["gear_type"],
                "name": values["name"],
                "notes": values["notes"],
            }
        )
        if self.fail_create_once:
            self.fail_create_once = False
            raise TimeoutError("Response lost after successful create")
        return SimpleNamespace(json=lambda: self.gear[-1])

    def add_gear_to_activity(self, target, activity_id):
        self.writes.append(("link", target, activity_id))
        self.links.setdefault(activity_id, []).extend(
            copy.deepcopy([g for g in self.gear if gear_uuid(g) == target])
        )
        if self.fail_link_once:
            self.fail_link_once = False
            raise TimeoutError("Response lost after successful link")

    def remove_gear_from_activity(self, target, activity_id):
        self.writes.append(("unlink", target, activity_id))
        self.links[activity_id] = [
            g for g in self.links[activity_id] if gear_uuid(g) != target
        ]


@pytest.fixture
def migration():
    client = FakeGarmin()
    source = SimpleNamespace()
    activities = [strava_activity()]
    gear = {
        "g1": {
            "id": "g1",
            "name": "Daily shoes",
            "brand_name": "Nike",
            "model_name": "Pegasus",
            "distance": 650000,
        }
    }

    source.identity = AsyncMock(return_value=42)
    source.coverage = AsyncMock(
        return_value={
            "source": "sqlite",
            "activities": 1,
            "first_activity_utc": "2026-10-20T06:00:00",
            "latest_activity_utc": "2026-10-20T06:00:00",
        }
    )
    source.activities = AsyncMock(side_effect=lambda *args: copy.deepcopy(activities))
    source.gear = AsyncMock(
        side_effect=lambda source_id: copy.deepcopy(gear[source_id])
    )
    garmin = SimpleNamespace(client=client, _ensure_connected=AsyncMock())
    return GearMigration(source, garmin, delay=0), activities, gear, client


def test_matching_requires_unique_time_sport_distance_and_duration():
    source = MigrationActivity.parse(strava_activity(), "strava")
    target = MigrationActivity.parse(garmin_activity(), "garmin")
    assert match_activities([source], [target])[1]["status"] == "matched"
    for values in [
        dict(startTimeGMT="2026-10-20 07:00:00"),
        dict(distance=12000),
        dict(elapsedDuration=5000),
        dict(activityType={"typeKey": "walking"}),
    ]:
        target = MigrationActivity.parse(garmin_activity(**values), "garmin")
        assert match_activities([source], [target])[1]["status"] == "unmatched"


def test_duplicate_targets_and_duplicate_sources_are_ambiguous():
    source = MigrationActivity.parse(strava_activity(), "strava")
    target = MigrationActivity.parse(garmin_activity(), "garmin")
    duplicate_target = MigrationActivity.parse(garmin_activity(102), "garmin")
    assert (
        match_activities([source], [target, duplicate_target])[1]["status"]
        == "ambiguous_activity"
    )
    duplicate_source = MigrationActivity.parse(strava_activity(2), "strava")
    assert all(
        m["status"] == "ambiguous_activity"
        for m in match_activities([source, duplicate_source], [target]).values()
    )


@pytest.mark.parametrize(
    "values",
    [dict(startTimeGMT=None), dict(distance=float("nan")), dict(elapsedDuration=0)],
)
def test_invalid_garmin_data_never_uses_current_or_local_time(values):
    with pytest.raises((ValueError, TypeError)):
        MigrationActivity.parse(garmin_activity(**values), "garmin")


@pytest.mark.asyncio
async def test_dry_run_only_reads_and_limits_calendar_dates(migration):
    sync, activities, gear, client = migration
    activities.extend(
        [
            strava_activity(2, start_date="2025-10-26T20:59:59Z"),
            strava_activity(3, start_date="2026-10-27T21:00:00Z"),
        ]
    )
    plan = await sync.build_plan(FIRST, LAST)
    assert client.writes == []
    assert plan["summary"] == {"link": 1}
    assert plan["strava_activities"] == 1
    assert plan["gear"]["g1"]["strava_lifetime_distance_m"] == 650000
    assert plan["gear"]["g1"]["strava_window_distance_m"] == 10000
    assert plan["gear"]["g1"]["first_use_date"] == "2026-10-20"


@pytest.mark.asyncio
async def test_creation_assignment_and_rerun_are_idempotent(migration):
    sync, activities, gear, client = migration
    events = []
    result = await sync.apply(await sync.build_plan(FIRST, LAST), events.append)
    assert result["created"] == result["linked"] == 1
    create = client.writes[0][1]
    assert create["notes"] == marker("g1", 42)
    assert create["activity_type_keys"] == []
    assert "max_usage_distance_km" not in create
    assert [event["event"] for event in events] == [
        "before_create",
        "created",
        "before_link",
        "linked",
    ]
    writes = copy.deepcopy(client.writes)
    result = await sync.apply(await sync.build_plan(FIRST, LAST), events.append)
    assert result["already_linked"] == 1
    assert client.writes == writes


@pytest.mark.asyncio
async def test_existing_gear_matched_by_name_and_uuid_formats(migration):
    sync, activities, gear, client = migration
    client.gear = [{"uuid": SHOE, "gearType": "SHOES", "name": "daily SHOES"}]
    plan = await sync.build_plan(FIRST, LAST)
    assert plan["gear"]["g1"]["target_uuid"] == SHOE.replace("-", "")
    result = await sync.apply(plan, lambda event: None)
    assert result["created"] == 0
    assert result["linked"] == 1


@pytest.mark.asyncio
async def test_ambiguous_gear_requires_explicit_mapping(migration):
    sync, activities, gear, client = migration
    client.gear = [
        {"uuid": ident, "gearType": "SHOES", "name": "Daily shoes"}
        for ident in [SHOE, OLD_SHOE]
    ]
    plan = await sync.build_plan(FIRST, LAST)
    assert plan["summary"] == {"ambiguous_gear": 1}
    assert (await sync.apply(plan, lambda event: None))["skipped"] == 1
    assert client.writes == []
    plan = await sync.build_plan(FIRST, LAST, mapping={"g1": SHOE})
    assert plan["summary"] == {"link": 1}
    with pytest.raises(ValueError):
        await sync.build_plan(FIRST, LAST, mapping={"g1": "unknown"})


@pytest.mark.asyncio
async def test_distinct_source_gear_must_not_merge_into_one_target(migration):
    sync, activities, gear, client = migration
    activities.append(
        strava_activity(2, gear_id="g2", start_date="2026-10-21T06:00:00Z")
    )
    client.activities.append(garmin_activity(102, startTimeGMT="2026-10-21 06:00:00"))
    gear["g2"] = {**gear["g1"], "id": "g2"}
    client.gear = [{"uuid": SHOE, "gearType": "SHOES", "name": "Daily shoes"}]
    plan = await sync.build_plan(FIRST, LAST)
    assert plan["summary"] == {"ambiguous_gear": 2}


@pytest.mark.asyncio
async def test_conflicts_are_skipped_until_replacement_is_requested(migration):
    sync, activities, gear, client = migration
    client.gear = [
        {"uuid": SHOE, "gearType": "SHOES", "name": "Daily shoes"},
        {"uuid": OLD_SHOE, "gearType": "SHOES", "name": "Other shoes"},
        {"uuid": COMPONENT, "gearType": "HELMET", "name": "Helmet"},
    ]
    client.links[101] = copy.deepcopy(client.gear[1:])
    plan = await sync.build_plan(FIRST, LAST)
    assert plan["summary"] == {"gear_conflict": 1}
    assert (await sync.apply(plan, lambda event: None))["skipped"] == 1
    assert client.writes == []
    result = await sync.apply(plan, lambda event: None, replace_existing=True)
    assert result["linked"] == result["replaced"] == 1
    assert {gear_uuid(g) for g in client.links[101]} == {
        SHOE.replace("-", ""),
        COMPONENT.replace("-", ""),
    }
    assert client.writes[0][0] == "link"
    assert client.writes[1][0] == "unlink"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fail_create_once", "fail_link_once"])
async def test_interrupted_successful_writes_can_resume_without_duplicates(
    migration, failure
):
    sync, activities, gear, client = migration
    setattr(client, failure, True)
    with pytest.raises(TimeoutError):
        await sync.apply(await sync.build_plan(FIRST, LAST), lambda event: None)
    await sync.apply(await sync.build_plan(FIRST, LAST), lambda event: None)
    assert len(client.gear) == len(client.links[101]) == 1
    assert sum(write[0] == "create" for write in client.writes) == 1
    assert sum(write[0] == "link" for write in client.writes) == 1


@pytest.mark.asyncio
async def test_account_change_aborts_before_writes(migration):
    sync, activities, gear, client = migration
    plan = await sync.build_plan(FIRST, LAST)
    sync.source.identity = AsyncMock(return_value=999)
    with pytest.raises(ValueError, match="account changed"):
        await sync.apply(plan, lambda event: None)
    assert client.writes == []


@pytest.mark.asyncio
async def test_failed_gear_read_never_turns_into_empty_gear(migration):
    sync, activities, gear, client = migration
    client.get_activity_gear_v2 = lambda activity_id: {"unexpected": []}
    with pytest.raises(ValueError, match="activity gear response"):
        await sync.build_plan(FIRST, LAST)
    assert client.writes == []


@pytest.mark.asyncio
async def test_existing_assignments_are_rechecked_before_apply(migration):
    sync, activities, gear, client = migration
    plan = await sync.build_plan(FIRST, LAST)
    client.links[101] = [{"uuid": OLD_SHOE, "gearType": "SHOES"}]
    result = await sync.apply(plan, lambda event: None)
    assert result["skipped"] == 1
    assert client.writes == []


@pytest.mark.asyncio
async def test_pagination_fetches_all_gear(migration):
    sync, activities, gear, client = migration
    client.gear = [
        {"uuid": f"{i:032x}", "gearType": "SHOES", "name": f"Gear {i}"}
        for i in range(101)
    ]
    assert len(await sync._gear_list()) == 101


@pytest.mark.asyncio
async def test_default_period_starts_with_first_garmin_activity_beyond_one_year(
    migration,
):
    sync, activities, gear, client = migration
    activities.append(strava_activity(2, start_date="2025-08-31T22:00:00Z"))
    client.activities.append(garmin_activity(102, startTimeGMT="2025-08-31 22:00:00"))
    plan = await sync.build_plan(None, LAST)
    assert plan["from_date"] == "2025-09-01"  # Calendar date in Moscow.
    assert plan["from_first_garmin_activity"] is True
    assert plan["summary"] == {"link": 2}
    assert plan["gear"]["g1"]["first_use_date"] == "2025-09-01"
    explicit = await sync.build_plan(FIRST, LAST)
    assert explicit["from_date"] == FIRST.isoformat()
    assert explicit["from_first_garmin_activity"] is False
    assert explicit["summary"] == {"link": 1}


@pytest.mark.asyncio
async def test_first_garmin_activity_is_found_on_later_pages(migration):
    sync, activities, gear, client = migration
    client.activities = [garmin_activity(1000 + index) for index in range(100)]
    client.activities.append(garmin_activity(2000, startTimeGMT="2025-09-01 06:00:00"))
    starts = []
    original = client.get_activities

    def page(start, limit):
        starts.append(start)
        return {"activityList": original(start, limit)}

    client.get_activities = page
    plan = await sync.build_plan(None, LAST)
    assert starts == [0, 100]
    assert plan["from_date"] == "2025-09-01"
    assert plan["garmin_activities"] == 101


@pytest.mark.asyncio
async def test_empty_garmin_has_no_invented_start_date(migration):
    sync, activities, gear, client = migration
    client.activities = []
    with pytest.raises(ValueError, match="no activities with a valid UTC start"):
        await sync.build_plan(None, LAST)
    assert client.writes == []


@pytest.mark.asyncio
async def test_cli_writes_report_and_journal_for_verified_assignments(
    migration, tmp_path, monkeypatch
):
    import json

    from app.config import settings
    from scripts import migrate_gear

    sync, activities, gear, client = migration
    monkeypatch.setattr(settings, "garmin_email", "test@example.com")
    monkeypatch.setattr(settings, "garmin_password", "test")
    monkeypatch.setattr(migrate_gear, "GearMigration", lambda **kwargs: sync)
    args = SimpleNamespace(
        timezone="Europe/Moscow",
        from_date=None,
        to_date=LAST,
        request_delay=0,
        gear_map=None,
        report=tmp_path / "report.json",
        journal=tmp_path / "journal.jsonl",
        apply=False,
        replace_existing=False,
    )
    assert await migrate_gear.run(args) == 0
    assert json.loads(args.report.read_text(encoding="utf-8"))["mode"] == "dry_run"
    assert not args.journal.exists()
    assert client.writes == []

    args.apply = True
    assert await migrate_gear.run(args) == 0
    report = json.loads(args.report.read_text(encoding="utf-8"))
    assert report["from_date"] == "2026-10-20"
    assert report["from_first_garmin_activity"] is True
    events = [
        json.loads(line)
        for line in args.journal.read_text(encoding="utf-8").splitlines()
    ]
    assert report["result"]["linked"] == 1
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "complete"
    assert all(event["strava_athlete_id"] == 42 for event in events)
