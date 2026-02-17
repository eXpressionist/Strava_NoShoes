
import asyncio
import json
import logging
import os
from datetime import time as dt_time
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.services.strava_service import StravaService
from app.utils.gpx_cleanup import cleanup_all_gpx_files
import re

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class BotService:
    def __init__(self):
        self.token = settings.bot_api_token
        self.state_file = settings.bot_state_file
        self.strava_service = StravaService()
        self.scheduler = AsyncIOScheduler()
        self.application = None
        
        # Load state
        self.state = self._load_state()
        
    def _load_state(self) -> dict:
        """Load bot state from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading bot state: {e}")
        return {"chat_id": None, "schedule_time": None}

    def _save_state(self):
        """Save bot state to file."""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f)
        except Exception as e:
            logger.error(f"Error saving bot state: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        chat_id = update.effective_chat.id
        self.state["chat_id"] = chat_id
        self._save_state()
        await update.message.reply_text(
            f"Hello! I am ready to check your Strava activities.\n"
            f"Use /check to run a manual check.\n"
            f"Use /schedule HH:MM to set a daily check time (e.g., /schedule 20:00)."
        )

    async def check_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /check command."""
        logger.info(f"Manual check triggered by {update.effective_chat.id}")
        await update.message.reply_text("Checking for activities without gear in the last 7 days...")
        # Manual check: last 7 days, report if empty
        await self.check_activities_without_gear(update.effective_chat.id, days_back=7, silent_if_empty=False)

    async def schedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /schedule command."""
        from datetime import datetime
        now = datetime.now()
        logger.info(f"Schedule command received from {update.effective_chat.id}. Server time: {now}")
        
        if not context.args:
            current_schedule = self.state.get("schedule_time")
            if current_schedule:
                await update.message.reply_text(
                    f"Current schedule: {current_schedule}\n"
                    f"To change it, provide a time in HH:MM format.\n"
                    f"Example: /schedule 20:00\n"
                    f"Server time: {now.strftime('%H:%M')}"
                )
            else:
                await update.message.reply_text(
                    f"No schedule set.\n"
                    f"Please provide a time in HH:MM format.\n"
                    f"Example: /schedule 20:00\n"
                    f"Server time: {now.strftime('%H:%M')}"
                )
            return

        time_str = context.args[0]
        try:
            hour, minute = map(int, time_str.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            
            self.state["schedule_time"] = f"{hour:02d}:{minute:02d}"
            self._save_state()
            
            # Reschedule
            self._schedule_job(hour, minute)
            
            await update.message.reply_text(
                f"Daily check scheduled for {hour:02d}:{minute:02d}.\n"
                f"Server time: {datetime.now().strftime('%H:%M')}"
            )
        except ValueError:
            await update.message.reply_text("Invalid time format. Please use HH:MM (24-hour).")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command."""
        self.state["schedule_time"] = None
        self._save_state()
        self.scheduler.remove_all_jobs()
        await update.message.reply_text("Daily schedule stopped.")
    
    async def handle_strava_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle messages containing Strava activity links."""
        message_text = update.message.text
        
        # Regex to extract Strava activity ID
        pattern = r'https://www\.strava\.com/activities/(\d+)'
        match = re.search(pattern, message_text)
        
        if not match:
            # Not a Strava link, ignore silently
            return
        
        activity_id = int(match.group(1))
        logger.info(f"Detected Strava activity link: {activity_id}")
        
        try:
            # Fetch activity details
            await update.message.reply_text("📊 Fetching activity details...")
            activity = await self.strava_service.get_activity_by_id(activity_id)
            
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
                [InlineKeyboardButton("📥 Download GPX", callback_data=f"gpx_{activity_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                message,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
            
        except Exception as e:
            logger.error(f"Error handling Strava link: {e}")
            await update.message.reply_text(
                f"❌ Error fetching activity details: {str(e)}"
            )
    
    async def gpx_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /gpx <activity_id> command."""
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "Usage: /gpx <activity_id>\n"
                "Example: /gpx 123456789"
            )
            return
        
        try:
            activity_id = int(context.args[0])
            chat_id = update.effective_chat.id
            await self.download_and_send_gpx(chat_id, activity_id)
        except ValueError:
            await update.message.reply_text("Invalid activity ID. Please provide a numeric ID.")
        except Exception as e:
            logger.error(f"Error in gpx_command: {e}")
            await update.message.reply_text(f"Error: {str(e)}")
    
    async def handle_gpx_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle callback query for GPX download button."""
        query = update.callback_query
        await query.answer()
        
        # Extract activity ID from callback data (format: "gpx_<activity_id>")
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
        """Download GPX file from Strava and send to user."""
        if not self.application:
            logger.warning("Bot application not initialized")
            return
        
        try:
            # Send progress message
            if query:
                await query.edit_message_text("📥 Downloading GPX file...")
            else:
                progress_msg = await self.application.bot.send_message(
                    chat_id=chat_id,
                    text="📥 Downloading GPX file..."
                )
            
            # Get activity details first
            activity = await self.strava_service.get_activity_by_id(activity_id)
            
            # Download GPX
            gpx_path = await self.strava_service.download_gpx(
                activity_id,
                activity_name=activity.name
            )
            
            # Send GPX file
            with open(gpx_path, 'rb') as gpx_file:
                await self.application.bot.send_document(
                    chat_id=chat_id,
                    document=gpx_file,
                    filename=f"{activity.name}.gpx",
                    caption=f"📊 GPX file for: {activity.name}"
                )
            
            # Update progress message
            success_msg = "✅ GPX file sent successfully!"
            if query:
                await query.edit_message_text(success_msg)
            else:
                await progress_msg.edit_text(success_msg)
            
            logger.info(f"Successfully sent GPX file for activity {activity_id} to chat {chat_id}")
            
        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            logger.error(f"Error downloading/sending GPX for activity {activity_id}: {e}")
            
            if query:
                await query.edit_message_text(error_msg)
            else:
                await self.application.bot.send_message(chat_id=chat_id, text=error_msg)

    def _schedule_job(self, hour: int, minute: int):
        """Schedule the daily check job."""
        self.scheduler.remove_all_jobs()
        self.scheduler.add_job(
            self.scheduled_check,
            CronTrigger(hour=hour, minute=minute),
            id="daily_check",
            replace_existing=True
        )
        logger.info(f"Scheduled job for {hour}:{minute}")

    async def scheduled_check(self):
        """The job that runs on schedule."""
        logger.info("Executed scheduled_check job")
        chat_id = self.state.get("chat_id")
        if chat_id:
            # Scheduled check: last 24 hours, silent if empty
            await self.check_activities_without_gear(chat_id, days_back=1, silent_if_empty=True)
        else:
            logger.warning("Scheduled check triggered but no chat_id found.")

    async def check_activities_without_gear(self, chat_id: int, days_back: int = None, silent_if_empty: bool = False):
        """Check for activities without gear and notify."""
        if not self.application:
            logger.warning("Bot application not initialized")
            return

        try:
            after_date = None
            if days_back:
                from datetime import timedelta, datetime
                # Use current time - days_back
                after_date = datetime.now() - timedelta(days=days_back)
                logger.info(f"Checking activities without gear for chat {chat_id}, days_back={days_back}, after={after_date}")

            activities = await self.strava_service.get_activities_without_gear(after=after_date)
            logger.info(f"Found {len(activities)} activities without gear")
            
            if not activities:
                logger.info(f"No activities without gear found. Silent mode: {silent_if_empty}")
                if not silent_if_empty:
                    await self.application.bot.send_message(chat_id=chat_id, text="All good! No activities without gear found in the specified period. 👍")
                else:
                    logger.info("Silent mode active, sending no message.")
                return

            time_msg = f" (last {days_back} days)" if days_back else ""
            message = f"⚠️ Found {len(activities)} activities without gear{time_msg}:\n\n"
            for activity in activities[:10]:  # Limit to 10 to avoid huge messages
                message += f"• <a href='https://www.strava.com/activities/{activity.id}'>{activity.name}</a> ({activity.start_date.strftime('%Y-%m-%d')})\n"
            
            if len(activities) > 10:
                message += f"\n...and {len(activities) - 10} more."

            await self.application.bot.send_message(
                chat_id=chat_id, 
                text=message, 
                parse_mode='HTML',
                disable_web_page_preview=True
            )
            
        except Exception as e:
            logger.error(f"Error in check_activities_without_gear: {e}")
            await self.application.bot.send_message(chat_id=chat_id, text=f"Error checking activities: {str(e)}")

    async def cleanup_gpx_job(self):
        """Scheduled job to clean up all GPX files."""
        logger.info("Running GPX cleanup job...")
        try:
            stats = cleanup_all_gpx_files(settings.gpx_storage_path)
            
            if stats['errors']:
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
        if not self.token or self.token == "your_telegram_bot_token_here":
            logger.warning("Bot token not configured. Telegram bot will not start.")
            return

        self.application = Application.builder().token(self.token).build()

        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("check", self.check_command))
        self.application.add_handler(CommandHandler("schedule", self.schedule_command))
        self.application.add_handler(CommandHandler("stop", self.stop_command))
        self.application.add_handler(CommandHandler("gpx", self.gpx_command))
        
        # Message handler for Strava links (non-command text messages)
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_strava_link)
        )
        
        # Callback query handler for inline buttons
        self.application.add_handler(CallbackQueryHandler(self.handle_gpx_callback))

        await self.application.initialize()
        await self.application.start()
        
        # Start the scheduler
        self.scheduler.start()
        
        # Schedule GPX cleanup if enabled
        if settings.gpx_cleanup_enabled:
            self.scheduler.add_job(
                self.cleanup_gpx_job,
                CronTrigger(
                    hour=settings.gpx_cleanup_schedule_hour,
                    minute=settings.gpx_cleanup_schedule_minute
                ),
                id="gpx_cleanup",
                replace_existing=True
            )
            logger.info(
                f"Scheduled GPX cleanup for {settings.gpx_cleanup_schedule_hour:02d}:"
                f"{settings.gpx_cleanup_schedule_minute:02d} daily"
            )
        
        # Restore schedule if exists
        stored_time = self.state.get("schedule_time")
        if stored_time:
            h, m = map(int, stored_time.split(':'))
            self._schedule_job(h, m)

        # We need to run polling in the background but it blocks, so we use updater or start/stop manually?
        # Application.run_polling() is blocking.
        # We should use start() and updater.start_polling() or just start polling
        
        await self.application.updater.start_polling()
        logger.info("Telegram bot started.")

    async def shutdown(self):
        """Shutdown the bot."""
        if self.scheduler.running:
            self.scheduler.shutdown()
        
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram bot stopped.")
