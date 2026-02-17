"""Application configuration using Pydantic Settings."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Strava API Configuration
    strava_client_id: str = Field(..., description="Strava API Client ID")
    strava_client_secret: str = Field(..., description="Strava API Client Secret")
    strava_access_token: str = Field(default="", description="Strava API Access Token")
    strava_refresh_token: str = Field(default="", description="Strava API Refresh Token")
    strava_token_expires_at: int = Field(default=0, description="Strava API Token Expiration Timestamp")
    strava_token_file: str = Field(default="data/strava_tokens.json", description="Path to store tokens")
    strava_api_base_url: str = Field(default="https://www.strava.com/api/v3", description="Strava API Base URL")
    
    # Telegram Bot Configuration
    bot_api_token: str = Field(default="", description="Telegram Bot API Token")
    bot_state_file: str = Field(default="data/bot_state.json", description="Path to store bot state")
    
    # Application Configuration
    app_host: str = Field(default="0.0.0.0", description="Application host")
    app_port: int = Field(default=8000, description="Application port")
    app_debug: bool = Field(default=False, description="Debug mode")
    
    # File Storage
    gpx_storage_path: str = Field(default="./data/gpx", description="Path to store GPX files")
    
    # GPX Cleanup Configuration
    gpx_cleanup_enabled: bool = Field(default=True, description="Enable GPX cleanup job")
    gpx_cleanup_schedule_hour: int = Field(default=3, description="Hour to run cleanup (0-23)")
    gpx_cleanup_schedule_minute: int = Field(default=0, description="Minute to run cleanup (0-59)")
    
    # Database (if needed in future)
    database_url: str = Field(default="sqlite:///./strava_noshoes.db", description="Database URL")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()