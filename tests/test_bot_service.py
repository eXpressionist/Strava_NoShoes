from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import bot_service
from app.services.bot_service import BotService


@pytest.fixture
def reminder_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bot_state_file", str(tmp_path / "bot.json"))
    monkeypatch.setattr(settings, "bot_timezone", "Europe/Moscow")
    monkeypatch.setattr(settings, "migration_enabled", True)
    monkeypatch.setattr(settings, "migration_cutoff", "2000-10-15")

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            now = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(bot_service, "datetime", FixedDateTime)
    bot = BotService()
    bot.application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    bot.state["chat_id"] = 123
    return bot


def activity(start_date, *, source="garmin", sport_type="Run", gear_id=None):
    return SimpleNamespace(
        source=source,
        start_date=start_date,
        sport_type=sport_type,
        gear_id=gear_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("gear_id", [None, "garmin-shoes"])
async def test_garmin_schedule_reminds_about_strava_regardless_of_garmin_gear(
    reminder_bot, monkeypatch, gear_id
):
    get_activities = AsyncMock(
        return_value=[activity(datetime(2026, 10, 23, 15), gear_id=gear_id)]
    )
    monkeypatch.setattr(reminder_bot.activity_service, "get_activities", get_activities)
    missing_gear = AsyncMock(side_effect=AssertionError("Do not infer Strava gear"))
    monkeypatch.setattr(
        reminder_bot.activity_service, "get_activities_without_gear", missing_gear
    )

    await reminder_bot.scheduled_check()

    reminder_bot.application.bot.send_message.assert_awaited_once()
    message = reminder_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Сегодня у вас была активность" in message
    assert "указали ли вы Gear" in message
    assert 'href="https://www.strava.com"' in message
    assert "без Gear" not in message
    missing_gear.assert_not_awaited()
    activity_filter = get_activities.call_args.args[0]
    assert activity_filter.after == datetime(2026, 10, 22, 21)
    assert activity_filter.before == datetime(2026, 10, 23, 21)
    assert activity_filter.has_gear is None
    assert get_activities.call_args.kwargs["include_gear"] is False


@pytest.mark.asyncio
async def test_reminder_uses_moscow_calendar_day_and_existing_sport_exemptions(
    reminder_bot, monkeypatch
):
    get_activities = AsyncMock(
        return_value=[
            activity(datetime(2026, 10, 22, 20, 59, 59)),  # Yesterday in Moscow.
            activity(datetime(2026, 10, 22, 21)),  # Midnight in Moscow.
            activity(datetime(2026, 10, 23, 17, tzinfo=timezone.utc)),
            activity(datetime(2026, 10, 23, 21)),  # Tomorrow in Moscow.
            activity(datetime(2026, 10, 23, 10), sport_type="WeightTraining"),
            activity(datetime(2026, 10, 23, 10), source="strava"),
        ]
    )
    monkeypatch.setattr(reminder_bot.activity_service, "get_activities", get_activities)

    await reminder_bot.scheduled_check()

    message = reminder_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Сегодня у вас были активности (2)" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("strength_only", [False, True])
async def test_no_scheduled_reminder_on_rest_or_strength_only_days(
    reminder_bot, monkeypatch, strength_only
):
    activities = (
        [activity(datetime(2026, 10, 23, 10), sport_type="WeightTraining")]
        if strength_only
        else []
    )
    monkeypatch.setattr(
        reminder_bot.activity_service,
        "get_activities",
        AsyncMock(return_value=activities),
    )

    await reminder_bot.scheduled_check()

    reminder_bot.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_check_uses_same_reminder_and_reports_empty_day(
    reminder_bot, monkeypatch
):
    monkeypatch.setattr(
        reminder_bot.activity_service, "get_activities", AsyncMock(return_value=[])
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )

    await reminder_bot.check_command(update, None)

    message = reminder_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Сегодня нет активностей" in message
    assert "Gear в Strava" in message


@pytest.mark.asyncio
async def test_secondary_keeps_existing_missing_gear_check(reminder_bot, monkeypatch):
    monkeypatch.setattr(settings, "migration_enabled", False)
    missing_gear = AsyncMock()
    reminder = AsyncMock()
    monkeypatch.setattr(reminder_bot, "check_activities_without_gear", missing_gear)
    monkeypatch.setattr(reminder_bot, "remind_strava_gear", reminder)

    await reminder_bot.scheduled_check()

    missing_gear.assert_awaited_once_with(123, days_back=1, silent_if_empty=True)
    reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_failure_does_not_claim_gear_is_missing(
    reminder_bot, monkeypatch
):
    monkeypatch.setattr(
        reminder_bot.activity_service,
        "get_activities",
        AsyncMock(side_effect=RuntimeError("Unavailable")),
    )

    await reminder_bot.scheduled_check()

    message = reminder_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Не удалось проверить" in message
    assert "указали ли вы Gear" not in message


def test_evening_schedule_uses_moscow_timezone(reminder_bot):
    reminder_bot._schedule_job(21, 0)
    trigger = reminder_bot.scheduler.get_job("daily_check").trigger
    next_run = trigger.get_next_fire_time(
        None, datetime(2026, 10, 23, 17, 59, tzinfo=timezone.utc)
    )

    assert next_run.hour == 21
    assert str(trigger.timezone) == "Europe/Moscow"
    assert next_run.astimezone(timezone.utc) == datetime(
        2026, 10, 23, 18, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_reminder_fetches_garmin_without_calling_gear_endpoint(
    reminder_bot, monkeypatch
):
    class Client:
        def get_activities_by_date(self, start_date, end_date):
            return [
                {
                    "activityId": 123,
                    "activityName": "Evening Run",
                    "activityType": {"typeKey": "running"},
                    "startTimeLocal": "2026-10-23 20:00:00",
                    "startTimeGMT": "2026-10-23 17:00:00",
                }
            ]

        def get_activity_gear(self, activity_id):
            raise AssertionError("Garmin gear is irrelevant to the reminder")

    reminder_bot.activity_service.garmin.client = Client()
    monkeypatch.setattr(reminder_bot.activity_service, "_ensure_db", AsyncMock())

    await reminder_bot.scheduled_check()

    message = reminder_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Сегодня у вас была активность" in message
