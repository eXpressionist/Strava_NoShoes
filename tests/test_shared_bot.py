import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import bot_service
from app.services.bot_service import BotService


def update_for(user_id, *, chat_id=None, chat_type="private", text=""):
    message = SimpleNamespace(reply_text=AsyncMock(), text=text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(
            id=user_id if chat_id is None else chat_id, type=chat_type
        ),
        effective_message=message,
        message=message,
        callback_query=None,
    )


@pytest.fixture
def shared_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bot_state_file", str(tmp_path / "bot.json"))
    monkeypatch.setattr(settings, "bot_primary_user_id", 101)
    monkeypatch.setattr(settings, "bot_secondary_user_id", 202)
    monkeypatch.setattr(settings, "bot_secondary_api_url", "http://secondary:8000")
    monkeypatch.setattr(settings, "bot_timezone", "Europe/Moscow")
    monkeypatch.setattr(settings, "bot_secondary_default_gear_by_sport", {})
    monkeypatch.setattr(settings, "migration_enabled", True)
    monkeypatch.setattr(settings, "migration_cutoff", "2000-10-15")
    bot = BotService()
    bot.application = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    )
    bot.user_services[202].get_activities = AsyncMock(return_value=[])
    return bot


@pytest.mark.asyncio
async def test_each_user_starts_and_schedules_independently(shared_bot):
    shared_bot.scheduler.add_job(AsyncMock(), "interval", hours=24, id="gpx_cleanup")
    for user_id, time in [(101, "21:00"), (202, "20:15")]:
        update = update_for(user_id)
        await shared_bot.start(update, None)
        await shared_bot.schedule_command(update, SimpleNamespace(args=[time]))

    users = shared_bot.state["users"]
    assert users["101"]["schedule_time"] == "21:00"
    assert users["202"]["schedule_time"] == "20:15"
    assert shared_bot.scheduler.get_job("daily_check_101").kwargs == {"chat_id": 101}
    assert shared_bot.scheduler.get_job("daily_check_202").kwargs == {"chat_id": 202}
    assert shared_bot.scheduler.get_job("gpx_cleanup") is not None

    await shared_bot.stop_command(update_for(202), None)

    assert shared_bot.scheduler.get_job("daily_check_101") is not None
    assert shared_bot.scheduler.get_job("daily_check_202") is None
    assert shared_bot.scheduler.get_job("gpx_cleanup") is not None
    assert users["101"]["schedule_time"] == "21:00"
    assert users["202"]["schedule_time"] is None

    restored = BotService()
    restored._restore_schedules()
    assert restored.scheduler.get_job("daily_check_101") is not None
    assert restored.scheduler.get_job("daily_check_202") is None


@pytest.mark.asyncio
async def test_scheduled_notifications_use_the_correct_backend_and_recipient(
    shared_bot, monkeypatch
):
    midnight = datetime.now(shared_bot.scheduler.timezone).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    started = (midnight + timedelta(hours=1)).astimezone(timezone.utc)
    primary_activity = SimpleNamespace(
        source="garmin", sport_type="Run", start_date=started, gear_id=None
    )
    secondary_activity = SimpleNamespace(
        source="strava", sport_type="Run", start_date=started, gear_id="wife-shoes"
    )
    primary = AsyncMock(return_value=[primary_activity])
    secondary = AsyncMock(return_value=[secondary_activity])
    monkeypatch.setattr(shared_bot.activity_service, "get_activities", primary)
    monkeypatch.setattr(shared_bot.user_services[202], "get_activities", secondary)

    await asyncio.gather(
        shared_bot.scheduled_check(101), shared_bot.scheduled_check(202)
    )

    messages = {
        call.kwargs["chat_id"]: call.kwargs["text"]
        for call in shared_bot.application.bot.send_message.call_args_list
    }
    assert "Сегодня у вас была активность" in messages[101]
    assert "указали ли вы Gear" in messages[101]
    assert "Сегодня у вас была активность" in messages[202]
    assert "правильно ли указана экипировка" in messages[202]
    assert shared_bot.application.bot.send_message.await_count == 2
    primary.assert_awaited_once()
    secondary.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_check_is_routed_per_user(shared_bot, monkeypatch):
    reminder, missing_gear = AsyncMock(), AsyncMock()
    monkeypatch.setattr(shared_bot, "remind_strava_gear", reminder)
    monkeypatch.setattr(shared_bot, "check_activities_without_gear", missing_gear)

    await shared_bot.check_command(update_for(101), None)
    await shared_bot.check_command(update_for(202), None)

    assert reminder.await_args_list == [
        call(101, silent_if_empty=False),
        call(202, silent_if_empty=False),
    ]
    missing_gear.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "start",
        "check_command",
        "schedule_command",
        "stop_command",
        "handle_activity_link",
        "gpx_command",
        "handle_gpx_callback",
    ],
)
async def test_unlinked_users_cannot_access_either_account(shared_bot, command):
    update = update_for(303, text="https://www.strava.com/activities/456")
    before = json.dumps(shared_bot.state, sort_keys=True)

    await getattr(shared_bot, command)(update, SimpleNamespace(args=["21:00"]))

    update.message.reply_text.assert_awaited_once()
    assert "нет привязки" in update.message.reply_text.call_args.args[0]
    assert json.dumps(shared_bot.state, sort_keys=True) == before
    shared_bot.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id,chat_type", [(-100, "group"), (202, "private")])
async def test_user_cannot_send_account_data_to_another_chat(
    shared_bot, chat_id, chat_type
):
    update = update_for(101, chat_id=chat_id, chat_type=chat_type)

    await shared_bot.check_command(update, None)

    assert "нет привязки" in update.message.reply_text.call_args.args[0]
    shared_bot.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_id_command_is_available_without_account_access(shared_bot):
    update = update_for(303)
    await shared_bot.id_command(update, None)
    assert "303" in update.message.reply_text.call_args.args[0]
    assert shared_bot.state.get("users", {}) == {}


def test_only_matching_legacy_subscription_is_migrated(shared_bot, tmp_path):
    state_file = tmp_path / "bot.json"
    state_file.write_text(json.dumps({"chat_id": 101, "schedule_time": "21:00"}))
    restored = BotService()
    restored._restore_schedules()
    assert restored.state["users"]["101"]["schedule_time"] == "21:00"
    assert restored.scheduler.get_job("daily_check_101") is not None
    assert restored.scheduler.get_job("daily_check_202") is None

    state_file.write_text(json.dumps({"chat_id": 303, "schedule_time": "21:00"}))
    restored = BotService()
    restored._restore_schedules()
    assert restored.scheduler.get_jobs() == []


@pytest.mark.asyncio
async def test_gpx_download_uses_subscriber_backend(shared_bot, tmp_path, monkeypatch):
    for user_id, prefix in [(101, "primary"), (202, "secondary")]:
        service = shared_bot._service_for_chat(user_id)
        path = tmp_path / f"{prefix}.gpx"
        path.write_text("<gpx/>")
        lookup = AsyncMock(return_value=SimpleNamespace(name=prefix))
        download = AsyncMock(return_value=str(path))
        monkeypatch.setattr(service, "get_activity_by_id", lookup)
        monkeypatch.setattr(service, "download_gpx", download)

        await shared_bot.download_and_send_gpx(user_id, 456)

        lookup.assert_awaited_once_with(456)
        download.assert_awaited_once_with(456, activity_name=prefix)
        document = shared_bot.application.bot.send_document.call_args.kwargs
        assert document["chat_id"] == user_id
        assert document["document"].name == str(path)


@pytest.mark.asyncio
async def test_disabled_instance_never_starts_polling(shared_bot, monkeypatch):
    monkeypatch.setattr(settings, "bot_enabled", False)
    monkeypatch.setattr(settings, "gpx_cleanup_enabled", True)
    builder = Mock(side_effect=AssertionError("Polling must stay disabled"))
    monkeypatch.setattr(bot_service.Application, "builder", builder)
    await shared_bot.initialize()
    builder.assert_not_called()
    assert shared_bot.scheduler.get_job("gpx_cleanup") is not None
    shared_bot.scheduler.shutdown(wait=False)


@pytest.mark.parametrize("primary,secondary", [(101, 0), (0, 202), (101, 101)])
def test_invalid_user_bindings_are_rejected(primary, secondary):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, bot_primary_user_id=primary, bot_secondary_user_id=secondary
        )


def secondary_activity(bot, **changes):
    midnight = datetime.now(bot.scheduler.timezone).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    fields = dict(
        source="strava",
        sport_type="Run",
        start_date=(midnight + timedelta(hours=1)).astimezone(timezone.utc),
        gear_id=None,
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sport_type,gear_id",
    [
        ("Run", None),
        ("Run", "old-shoes"),
        ("Run", "new-shoes"),
        ("TrailRun", "trail-shoes"),
        ("Ride", "bike"),
    ],
)
async def test_secondary_reminds_regardless_of_gear_and_old_defaults(
    shared_bot, monkeypatch, sport_type, gear_id
):
    monkeypatch.setattr(
        settings, "bot_secondary_default_gear_by_sport", {"Run": "old-shoes"}
    )
    service = shared_bot.user_services[202]
    service.get_activities.return_value = [
        secondary_activity(shared_bot, sport_type=sport_type, gear_id=gear_id)
    ]
    service.get_activities_without_gear = AsyncMock()
    service.get_gear_by_id = AsyncMock()

    await shared_bot.scheduled_check(202)

    shared_bot.application.bot.send_message.assert_awaited_once()
    message = shared_bot.application.bot.send_message.call_args.kwargs
    assert message["chat_id"] == 202
    assert "правильно ли указана экипировка" in message["text"]
    assert 'href="https://www.strava.com"' in message["text"]
    assert "по умолчанию" not in message["text"]
    assert "без Gear" not in message["text"]
    service.get_activities_without_gear.assert_not_awaited()
    service.get_gear_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_secondary_reminder_uses_moscow_calendar_day(shared_bot):
    midnight = datetime.now(shared_bot.scheduler.timezone).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    service = shared_bot.user_services[202]
    service.get_activities.return_value = [
        secondary_activity(shared_bot, start_date=midnight - timedelta(seconds=1)),
        secondary_activity(shared_bot, start_date=midnight),
        secondary_activity(shared_bot),
        secondary_activity(shared_bot, start_date=midnight + timedelta(days=1)),
        secondary_activity(shared_bot, sport_type="WeightTraining"),
        secondary_activity(shared_bot, source="garmin"),
    ]

    await shared_bot.scheduled_check(202)

    message = shared_bot.application.bot.send_message.call_args.kwargs["text"]
    assert "Сегодня у вас были активности (2)" in message
    activity_filter = service.get_activities.call_args.args[0]
    assert activity_filter.after == midnight - timedelta(seconds=1)
    assert activity_filter.before == midnight + timedelta(days=1)
    assert activity_filter.after.utcoffset() == timedelta(0)
    assert activity_filter.has_gear is None
    assert activity_filter.gear_id is None
    assert service.get_activities.call_args.kwargs == {
        "all_pages": True,
        "include_gear": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("strength_only", [False, True])
async def test_secondary_is_quiet_without_eligible_activities(
    shared_bot, strength_only
):
    shared_bot.user_services[202].get_activities.return_value = (
        [secondary_activity(shared_bot, sport_type="WeightTraining")]
        if strength_only
        else []
    )

    await shared_bot.scheduled_check(202)

    shared_bot.application.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_secondary_manual_check_reports_empty_day(shared_bot):
    await shared_bot.check_command(update_for(202), None)

    shared_bot.application.bot.send_message.assert_awaited_once()
    message = shared_bot.application.bot.send_message.call_args.kwargs
    assert message["chat_id"] == 202
    assert "Сегодня нет активностей" in message["text"]


@pytest.mark.asyncio
async def test_secondary_failure_does_not_report_a_workout(shared_bot):
    shared_bot.user_services[202].get_activities.side_effect = RuntimeError(
        "Unavailable"
    )

    await shared_bot.scheduled_check(202)

    shared_bot.application.bot.send_message.assert_awaited_once()
    message = shared_bot.application.bot.send_message.call_args.kwargs
    assert message["chat_id"] == 202
    assert "Не удалось проверить" in message["text"]
    assert "была активность" not in message["text"]


def test_obsolete_default_gear_setting_still_loads(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text('BOT_SECONDARY_DEFAULT_GEAR_BY_SPORT={"Run":"old-shoes"}\n')
    Settings(_env_file=path)
