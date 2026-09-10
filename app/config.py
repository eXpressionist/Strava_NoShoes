"""Application configuration using Pydantic Settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False
    )

    # Strava API Configuration (primary activity data source)
    strava_client_id: str = Field(default="", description="Strava API Client ID")
    strava_client_secret: str = Field(
        default="", description="Strava API Client Secret"
    )
    strava_access_token: str = Field(default="", description="Strava API Access Token")
    strava_refresh_token: str = Field(
        default="", description="Strava API Refresh Token"
    )
    strava_token_expires_at: int = Field(
        default=0, description="Strava API Token Expiration Timestamp"
    )
    strava_token_file: str = Field(
        default="data/strava_tokens.json", description="Path to store tokens"
    )
    strava_api_base_url: str = Field(
        default="https://www.strava.com/api/v3", description="Strava API Base URL"
    )

    # Garmin becomes the live activity source after the Strava cutoff.
    garmin_email: str = Field(default="", description="Garmin Connect email/login")
    garmin_password: str = Field(default="", description="Garmin Connect password")
    garmin_token_store: str = Field(
        default="data/garmin_tokens",
        description="Directory to store Garmin session tokens",
    )

    # Telegram Bot Configuration
    bot_api_token: str = Field(default="", description="Telegram Bot API Token")
    bot_state_file: str = Field(
        default="data/bot_state.json", description="Path to store bot state"
    )

    # Application Configuration
    app_host: str = Field(default="0.0.0.0", description="Application host")
    app_port: int = Field(default=8000, description="Application port")
    app_debug: bool = Field(default=False, description="Debug mode")

    # File Storage
    gpx_storage_path: str = Field(
        default="./data/gpx", description="Path to store GPX files"
    )

    # GPX Cleanup Configuration
    gpx_cleanup_enabled: bool = Field(
        default=True, description="Enable GPX cleanup job"
    )
    gpx_cleanup_schedule_hour: int = Field(
        default=3, description="Hour to run cleanup (0-23)"
    )
    gpx_cleanup_schedule_minute: int = Field(
        default=0, description="Minute to run cleanup (0-59)"
    )

    # Database
    database_url: str = Field(
        default="sqlite:///./strava_noshoes.db", description="Database URL"
    )

    # Strava backup and source cutover. The cutoff day is inclusive: Strava is
    # backed up and used through 2026-10-15, Garmin is used from 2026-10-16.
    migration_cutoff: str = Field(
        default="2026-10-15",
        description="Last day Strava is used and backed up (YYYY-MM-DD)",
    )
    strava_backup_enabled: bool = Field(
        default=True, description="Run automatic Strava-to-SQLite backups"
    )
    strava_backup_on_startup: bool = Field(
        default=True, description="Start an incremental backup when the app starts"
    )
    strava_backup_schedule_hour: int = Field(
        default=4, description="Daily Strava backup hour (server local time)"
    )
    strava_backup_schedule_minute: int = Field(
        default=15, description="Daily Strava backup minute"
    )
    strava_backup_streams_per_run: int = Field(
        default=60,
        ge=0,
        description="Maximum activity stream downloads per backup run",
    )
    garmin_sync_enabled: bool = Field(
        default=True,
        description="Sync Garmin activities to SQLite after the migration cutoff",
    )
    garmin_sync_lookback_days: int = Field(
        default=7,
        ge=1,
        description="Days to re-sync so late Garmin gear edits are captured",
    )


# Global settings instance
settings = Settings()
