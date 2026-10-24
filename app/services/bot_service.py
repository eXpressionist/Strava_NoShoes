import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.models.strava import ActivityFilter
from app.services.gear_policy import requires_gear
from app.services.remote_activity_service import RemoteActivityService
from app.services.unified_service import UnifiedActivityService
from app.utils.gpx_cleanup import cleanup_all_gpx_files

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


class BotService:
    def __init__(self):
        self.token = settings.bot_api_token
        self.state_file = settings.bot_state_file
        self.activity_service = UnifiedActivityService()
        self.scheduler = AsyncIOScheduler(timezone=settings.bot_timezone or None)
        self.application = None
        self.user_services = {}
        if settings.bot_primary_user_id and settings.bot_secondary_user_id:
            self.user_services = {
                settings.bot_primary_user_id: self.activity_service,
                settings.bot_secondary_user_id: RemoteActivityService(
                    settings.bot_secondary_api_url
                ),
            }

        # Load state
        self.state = self._load_state()
        legacy_chat_id = self.state.get("chat_id")
        if legacy_chat_id in self.user_services:
            self.state.setdefault("users", {}).setdefault(
                str(legacy_chat_id),
                {
                    "chat_id": legacy_chat_id,
                    "schedule_time": self.state.get("schedule_time"),
                },
            )

    def _service_for_chat(self, chat_id: int):
        if not self.user_services:
            return self.activity_service
        if chat_id not in self.user_services:
            raise PermissionError("Telegram user is not linked to an account")
        return self.user_services[chat_id]

    def _uses_gear_reminder(self, chat_id: int) -> bool:
        return (
            bool(self.user_services)
            or not self._service_for_chat(chat_id).strava_is_live()
        )

    def _subscriber_state(self, chat_id: int) -> dict:
        self._service_for_chat(chat_id)
        if not self.user_services:
            return self.state
        return self.state.setdefault("users", {}).setdefault(
            str(chat_id), {"chat_id": chat_id, "schedule_time": None}
        )

    def _job_id(self, chat_id: Optional[int] = None) -> str:
        return f"daily_check_{chat_id}" if self.user_services else "daily_check"

    async def _authorize(self, update: Update) -> bool:
        if not self.user_services:
            return True
        user, chat = update.effective_user, update.effective_chat
        if (
            user
            and chat
            and chat.type == "private"
            and user.id == chat.id
            and user.id in self.user_services
        ):
            return True
        message = "Для вашего Telegram ID нет привязки. Используйте /id в личном чате."
        if update.callback_query:
            await update.callback_query.answer(message, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(message)
        return False

    async def id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user:
            await update.effective_message.reply_text(
                f"Ваш Telegram user ID: {update.effective_user.id}"
            )

    def _load_state(self) -> dict:
        """Load bot state from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading bot state: {e}")
        return {"chat_id": None, "schedule_time": None}

    def _save_state(self):
        """Save bot state to file."""
        temporary_file = self.state_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(temporary_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(temporary_file, self.state_file)
        except Exception as e:
            logger.error(f"Error saving bot state: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        if not await self._authorize(update):
            return
        chat_id = update.effective_chat.id
        self._subscriber_state(chat_id)["chat_id"] = chat_id
        self._save_state()
        data_source = (
            "Strava"
            if self._service_for_chat(chat_id).strava_is_live()
            else f"Strava through {settings.migration_cutoff}, then Garmin"
        )
        await update.message.reply_text(
            f"Hello! I am ready to check your activities.\n"
            f"Data source: {data_source}.\n\n"
            f"Use /check to run a manual check.\n"
            f"Use /schedule HH:MM to set a daily check time (e.g., /schedule 20:00)."
        )

    async def check_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /check command."""
        if not await self._authorize(update):
            return
        logger.info(f"Manual check triggered by {update.effective_chat.id}")
        if self._uses_gear_reminder(update.effective_chat.id):
            await update.message.reply_text(
                "Проверяю сегодняшние активности для напоминания о Gear в Strava..."
            )
            await self.remind_strava_gear(
                update.effective_chat.id, silent_if_empty=False
            )
            return
        await update.message.reply_text(
            "Checking for activities without gear in the last 7 days..."
        )
        # Manual check: last 7 days, report if empty
        await self.check_activities_without_gear(
            update.effective_chat.id, days_back=7, silent_if_empty=False
        )

    async def schedule_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle /schedule command."""
        if not await self._authorize(update):
            return
        chat_id = update.effective_chat.id
        subscriber = self._subscriber_state(chat_id)
        now = datetime.now(self.scheduler.timezone)
        logger.info(
            f"Schedule command received from {update.effective_chat.id}. Server time: {now}"
        )

        if not context.args:
            current_schedule = subscriber.get("schedule_time")
            if current_schedule:
                await update.message.reply_text(
                    f"Current schedule: {current_schedule}\n"
                    f"To change it, provide a time in HH:MM format.\n"
                    f"Example: /schedule 20:00\n"
                    f"Bot time: {now.strftime('%H:%M')} ({self.scheduler.timezone})"
                )
            else:
                await update.message.reply_text(
                    f"No schedule set.\n"
                    f"Please provide a time in HH:MM format.\n"
                    f"Example: /schedule 20:00\n"
                    f"Bot time: {now.strftime('%H:%M')} ({self.scheduler.timezone})"
                )
            return

        time_str = context.args[0]
        try:
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError

            subscriber["chat_id"] = chat_id
            subscriber["schedule_time"] = f"{hour:02d}:{minute:02d}"
            self._save_state()

            # Reschedule
            self._schedule_job(hour, minute, chat_id=chat_id)

            await update.message.reply_text(
                f"Daily check scheduled for {hour:02d}:{minute:02d}.\n"
                f"Bot time: {now.strftime('%H:%M')} ({self.scheduler.timezone})"
            )
        except ValueError:
            await update.message.reply_text(
                "Invalid time format. Please use HH:MM (24-hour)."
            )

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command."""
        if not await self._authorize(update):
            return
        chat_id = update.effective_chat.id
        self._subscriber_state(chat_id)["schedule_time"] = None
        self._save_state()
        job_id = self._job_id(chat_id)
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        await update.message.reply_text("Daily schedule stopped.")

    async def handle_activity_link(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle messages containing Garmin or Strava activity links."""
        if not await self._authorize(update):
            return
        message_text = update.message.text

        garmin_pattern = r"https://connect\.garmin\.com/modern/activity/(\d+)"
        strava_pattern = r"https://www\.strava\.com/activities/(\d+)"

        garmin_match = re.search(garmin_pattern, message_text)
        strava_match = re.search(strava_pattern, message_text)
        match = garmin_match or strava_match

        if not match:
            return

        activity_id = int(match.group(1))
        logger.info(f"Detected activity link: {activity_id}")

        try:
            await update.message.reply_text("📊 Fetching activity details...")
            source = "garmin" if garmin_match else "strava"
            activity = await self._service_for_chat(
                update.effective_chat.id
            ).get_activity_by_id(activity_id, source=source)

            # Format activity details
            distance_km = activity.distance / 1000
            duration_min = activity.moving_time / 60
            pace_str = ""

            if activity.average_speed and activity.average_speed > 0:
                pace_sec_per_km = 1000 / activity.average_speed
                pace_min = int(pace_sec_per_km // 60)
                pace_sec = int(pace_sec_per_km % 60)
                pace_str = f"\n📈 Pace: {pace_min}:{pace_sec:02d} min/km"

            message = (
                f"🏃 <b>{activity.name}</b>\n"
                f"📅 {activity.start_date.strftime('%Y-%m-%d %H:%M')}\n"
                f"🏷 Type: {activity.sport_type}\n"
                f"📏 Distance: {distance_km:.2f} km\n"
                f"⏱ Time: {int(duration_min)} min"
                f"{pace_str}\n"
            )

            if activity.gear_id:
                gear_name = activity.gear_name or activity.gear_id
                message += f"\n👟 Gear: {gear_name}"

            # Create inline keyboard with GPX download button
            keyboard = [
                [
                    InlineKeyboardButton(
                        "📥 Download GPX", callback_data=f"gpx_{activity_id}"
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                message, parse_mode="HTML", reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Error handling activity link: {e}")
            await update.message.reply_text(
                f"❌ Error fetching activity details: {str(e)}"
            )

    async def gpx_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /gpx <activity_id> command."""
        if not await self._authorize(update):
            return
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "Usage: /gpx <activity_id>\n" "Example: /gpx 123456789"
            )
            return

        try:
            activity_id = int(context.args[0])
            chat_id = update.effective_chat.id
            await self.download_and_send_gpx(chat_id, activity_id)
        except ValueError:
            await update.message.reply_text(
                "Invalid activity ID. Please provide a numeric ID."
            )
        except Exception as e:
            logger.error(f"Error in gpx_command: {e}")
            await update.message.reply_text(f"Error: {str(e)}")

    async def handle_gpx_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle callback query for GPX download button."""
        if not await self._authorize(update):
            return
        query = update.callback_query
        await query.answer()

        callback_data = query.data
        if not callback_data.startswith("gpx_"):
            await query.edit_message_text("Invalid callback data.")
            return

        try:
            activity_id = int(callback_data.split("_")[1])
            chat_id = query.message.chat_id
            await self.download_and_send_gpx(chat_id, activity_id, query)
        except Exception as e:
            logger.error(f"Error in handle_gpx_callback: {e}")
            await query.edit_message_text(f"❌ Error: {str(e)}")

    async def download_and_send_gpx(self, chat_id: int, activity_id: int, query=None):
        """Download GPX file and send to user."""
        if not self.application:
            logger.warning("Bot application not initialized")
            return

        try:
            activity_service = self._service_for_chat(chat_id)
            if query:
                await query.edit_message_text("📥 Downloading GPX file...")
            else:
                progress_msg = await self.application.bot.send_message(
                    chat_id=chat_id, text="📥 Downloading GPX file..."
                )

            # Get activity details first
            activity = await activity_service.get_activity_by_id(activity_id)

            # Download GPX through Strava streams.
            gpx_path = await activity_service.download_gpx(
                activity_id, activity_name=activity.name
            )

            # Send GPX file
            with open(gpx_path, "rb") as gpx_file:
                await self.application.bot.send_document(
                    chat_id=chat_id,
                    document=gpx_file,
                    filename=f"{activity.name}.gpx",
                    caption=f"📊 GPX file for: {activity.name}",
                )

            success_msg = "✅ GPX file sent successfully!"
            if query:
                await query.edit_message_text(success_msg)
            else:
                await progress_msg.edit_text(success_msg)

            logger.info(
                f"Successfully sent GPX file for activity {activity_id} to chat {chat_id}"
            )

        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            logger.error(
                f"Error downloading/sending GPX for activity {activity_id}: {e}"
            )

            if query:
                await query.edit_message_text(error_msg)
            else:
                await self.application.bot.send_message(chat_id=chat_id, text=error_msg)

    def _schedule_job(self, hour: int, minute: int, chat_id: Optional[int] = None):
        """Schedule the daily check job."""
        if self.user_services:
            self._service_for_chat(chat_id)
        self.scheduler.add_job(
            self.scheduled_check,
            CronTrigger(hour=hour, minute=minute, timezone=self.scheduler.timezone),
            id=self._job_id(chat_id),
            kwargs={"chat_id": chat_id} if self.user_services else {},
            replace_existing=True,
        )
        logger.info(f"Scheduled job for {hour}:{minute}")

    async def scheduled_check(self, chat_id: Optional[int] = None):
        """The job that runs on schedule."""
        logger.info("Executed scheduled_check job")
        if not self.user_services:
            chat_id = chat_id or self.state.get("chat_id")
        elif chat_id not in self.user_services:
            logger.warning("Scheduled check has no linked subscriber")
            return
        if chat_id:
            if self._uses_gear_reminder(chat_id):
                await self.remind_strava_gear(chat_id)
            else:
                await self.check_activities_without_gear(
                    chat_id, days_back=1, silent_if_empty=True
                )
        else:
            logger.warning("Scheduled check triggered but no chat_id found.")

    async def remind_strava_gear(self, chat_id: int, silent_if_empty: bool = True):
        """Remind users to check today's gear in Strava, without inferring it."""
        if not self.application:
            logger.warning("Bot application not initialized")
            return

        try:
            service = self._service_for_chat(chat_id)
            source = "strava" if service.strava_is_live() else "garmin"
            now = datetime.now(self.scheduler.timezone)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            after = day_start.astimezone(timezone.utc)
            before = day_end.astimezone(timezone.utc)
            if source == "strava":
                # Strava's "after" is exclusive; include midnight for filtering.
                after -= timedelta(seconds=1)
            else:
                # Garmin and the local archive use naive UTC timestamps.
                after = after.replace(tzinfo=None)
                before = before.replace(tzinfo=None)
            activity_filter = ActivityFilter(
                after=after,
                before=before,
            )
            activities = await service.get_activities(
                activity_filter, all_pages=True, include_gear=False
            )

            activities_today = []
            for activity in activities:
                if activity.source != source or not requires_gear(activity):
                    continue
                started_at = activity.start_date
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                if day_start <= started_at < day_end:
                    activities_today.append(activity)

            if not activities_today:
                if not silent_if_empty:
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text="Сегодня нет активностей, для которых нужно проверить Gear в Strava.",
                    )
                return

            activity_text = (
                "Сегодня у вас была активность."
                if len(activities_today) == 1
                else f"Сегодня у вас были активности ({len(activities_today)})."
            )
            gear_prompt = (
                "правильно ли указана экипировка."
                if source == "strava"
                else "указали ли вы Gear."
            )
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🏃 {activity_text}\n"
                    'Проверьте в <a href="https://www.strava.com">Strava</a>, '
                    f"{gear_prompt}"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Could not check today's activities for a Strava reminder")
            await self.application.bot.send_message(
                chat_id=chat_id,
                text="Не удалось проверить сегодняшние активности. Попробуйте /check позже.",
            )

    async def check_activities_without_gear(
        self, chat_id: int, days_back: int = None, silent_if_empty: bool = False
    ):
        """Check for activities without gear and notify."""
        if not self.application:
            logger.warning("Bot application not initialized")
            return

        try:
            after_date = None
            if days_back:
                from datetime import datetime, timedelta

                after_date = datetime.now() - timedelta(days=days_back)
                logger.info(
                    f"Checking activities without gear for chat {chat_id}, days_back={days_back}, after={after_date}"
                )

            activities = await self._service_for_chat(
                chat_id
            ).get_activities_without_gear(after=after_date)
            logger.info(f"Found {len(activities)} activities without gear")

            if not activities:
                logger.info(
                    f"No activities without gear found. Silent mode: {silent_if_empty}"
                )
                if not silent_if_empty:
                    await self.application.bot.send_message(
                        chat_id=chat_id,
                        text="All good! No activities without gear found in the specified period. 👍",
                    )
                else:
                    logger.info("Silent mode active, sending no message.")
                return

            time_msg = f" (last {days_back} days)" if days_back else ""
            message = (
                f"⚠️ Found {len(activities)} activities without gear{time_msg}:\n\n"
            )
            for activity in activities[:10]:
                if activity.source == "garmin":
                    link = f"https://connect.garmin.com/modern/activity/{activity.id}"
                else:
                    link = f"https://www.strava.com/activities/{activity.id}"
                message += f"• <a href='{link}'>{activity.name}</a> ({activity.start_date.strftime('%Y-%m-%d')})\n"

            if len(activities) > 10:
                message += f"\n...and {len(activities) - 10} more."

            await self.application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

        except Exception as e:
            logger.error(f"Error in check_activities_without_gear: {e}")
            await self.application.bot.send_message(
                chat_id=chat_id, text=f"Error checking activities: {str(e)}"
            )

    async def cleanup_gpx_job(self):
        """Scheduled job to clean up all GPX files."""
        logger.info("Running GPX cleanup job...")
        try:
            stats = cleanup_all_gpx_files(settings.gpx_storage_path)

            if stats["errors"]:
                logger.warning(f"GPX cleanup completed with errors: {stats['errors']}")
            else:
                logger.info(
                    f"GPX cleanup successful: {stats['files_deleted']} file(s) deleted, "
                    f"{stats['space_freed_human']} freed"
                )
        except Exception as e:
            logger.error(f"Error during GPX cleanup job: {e}")

    async def initialize(self):
        """Initialize the bot application."""
        if not settings.bot_enabled:
            self._schedule_cleanup()
            logger.info("Telegram polling is disabled for this instance")
            return
        if not self.token or self.token == "your_telegram_bot_token_here":
            logger.warning("Bot token not configured. Telegram bot will not start.")
            return

        self.application = Application.builder().token(self.token).build()

        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("id", self.id_command))
        self.application.add_handler(CommandHandler("check", self.check_command))
        self.application.add_handler(CommandHandler("schedule", self.schedule_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("gpx", self.gpx_command))

        # Message handler for Strava activity links.
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_activity_link)
        )

        # Callback query handler for inline buttons
        self.application.add_handler(CallbackQueryHandler(self.handle_gpx_callback))

        await self.application.initialize()
        await self.application.start()

        # Start the scheduler
        self.scheduler.start()

        self._schedule_cleanup()
        self._restore_schedules()

        await self.application.updater.start_polling()
        logger.info("Telegram bot started.")

    def _schedule_cleanup(self):
        """Keep backend file cleanup active even when Telegram polling is disabled."""
        if settings.gpx_cleanup_enabled:
            if not self.scheduler.running:
                self.scheduler.start()
            self.scheduler.add_job(
                self.cleanup_gpx_job,
                CronTrigger(
                    hour=settings.gpx_cleanup_schedule_hour,
                    minute=settings.gpx_cleanup_schedule_minute,
                ),
                id="gpx_cleanup",
                replace_existing=True,
            )
            logger.info(
                f"Scheduled GPX cleanup for {settings.gpx_cleanup_schedule_hour:02d}:"
                f"{settings.gpx_cleanup_schedule_minute:02d} daily"
            )

    def _restore_schedules(self):
        subscribers = (
            [
                (chat_id, self.state.get("users", {}).get(str(chat_id), {}))
                for chat_id in self.user_services
            ]
            if self.user_services
            else [(self.state.get("chat_id"), self.state)]
        )
        for chat_id, subscriber in subscribers:
            stored_time = subscriber.get("schedule_time")
            if chat_id and stored_time:
                if not isinstance(stored_time, str):
                    logger.warning("Ignoring invalid saved schedule for %s", chat_id)
                    continue
                try:
                    hour, minute = map(int, stored_time.split(":"))
                    self._schedule_job(hour, minute, chat_id=chat_id)
                except (TypeError, ValueError):
                    logger.warning("Ignoring invalid saved schedule for %s", chat_id)

    async def shutdown(self):
        """Shutdown the bot."""
        if self.scheduler.running:
            self.scheduler.shutdown()

        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram bot stopped.")
