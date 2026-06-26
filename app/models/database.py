"""SQLite database models and setup using SQLAlchemy async."""

import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, Integer, Float, String, Boolean, DateTime, Text, ForeignKey,
    create_engine, Index
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import settings


class Base(DeclarativeBase):
    pass


class ActivityDB(Base):
    """Activities table — stores both Strava backup and Garmin activities."""
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Source identification
    source = Column(String(20), nullable=False)  # "strava" or "garmin"
    source_id = Column(String(50), nullable=False)  # Original ID from source

    # Core fields
    name = Column(String(255), nullable=False)
    sport_type = Column(String(50), nullable=False)
    activity_type = Column(String(50), nullable=True)  # Legacy Strava "type" field

    # Metrics
    distance = Column(Float, default=0.0)  # meters
    moving_time = Column(Integer, default=0)  # seconds
    elapsed_time = Column(Integer, default=0)  # seconds
    total_elevation_gain = Column(Float, default=0.0)  # meters
    average_speed = Column(Float, default=0.0)  # m/s
    max_speed = Column(Float, default=0.0)  # m/s
    average_heartrate = Column(Float, nullable=True)
    max_heartrate = Column(Float, nullable=True)
    average_cadence = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)

    # Time
    start_date = Column(DateTime, nullable=False)
    start_date_local = Column(DateTime, nullable=False)
    timezone = Column(String(100), nullable=True)

    # Location
    start_lat = Column(Float, nullable=True)
    start_lng = Column(Float, nullable=True)
    end_lat = Column(Float, nullable=True)
    end_lng = Column(Float, nullable=True)

    # Gear
    gear_id = Column(String(50), nullable=True)
    gear_name = Column(String(255), nullable=True)

    # Elevation
    elev_high = Column(Float, nullable=True)
    elev_low = Column(Float, nullable=True)

    # Flags
    trainer = Column(Boolean, default=False)
    manual = Column(Boolean, default=False)
    private = Column(Boolean, default=False)
    has_heartrate = Column(Boolean, default=False)

    # GPX availability
    has_gps_data = Column(Boolean, default=False)
    gpx_file_path = Column(String(500), nullable=True)

    # Raw data (JSON dump of original API response for future reference)
    raw_data = Column(Text, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    streams = relationship("ActivityStreamDB", back_populates="activity", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_source_source_id", "source", "source_id", unique=True),
        Index("idx_start_date", "start_date"),
        Index("idx_sport_type", "sport_type"),
        Index("idx_gear_id", "gear_id"),
    )


class GearDB(Base):
    """Gear/equipment table."""
    __tablename__ = "gear"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Source identification
    source = Column(String(20), nullable=False)  # "strava" or "garmin"
    source_id = Column(String(50), nullable=False)  # Original gear ID

    # Core fields
    name = Column(String(255), nullable=False)
    gear_type = Column(String(50), nullable=True)  # "shoes", "bike", etc.
    brand = Column(String(100), nullable=True)
    model = Column(String(100), nullable=True)

    # Stats
    distance = Column(Float, default=0.0)  # total distance in meters
    activities_count = Column(Integer, default=0)

    # Status
    retired = Column(Boolean, default=False)
    primary = Column(Boolean, default=False)

    # Metadata
    date_begin = Column(DateTime, nullable=True)
    date_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_gear_source_id", "source", "source_id", unique=True),
    )


class ActivityStreamDB(Base):
    """Activity GPS/sensor streams for GPX generation."""
    __tablename__ = "activity_streams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(Integer, ForeignKey("activities.id", ondelete="CASCADE"), nullable=False)

    # Stream point index
    point_index = Column(Integer, nullable=False)

    # GPS
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    altitude = Column(Float, nullable=True)

    # Time offset from start (seconds)
    time_offset = Column(Integer, nullable=True)

    # Sensors
    heartrate = Column(Integer, nullable=True)
    cadence = Column(Integer, nullable=True)
    temperature = Column(Float, nullable=True)

    # Relationships
    activity = relationship("ActivityDB", back_populates="streams")

    __table_args__ = (
        Index("idx_stream_activity", "activity_id", "point_index"),
    )


# Database engine setup
def get_database_url() -> str:
    """Get async database URL from settings."""
    url = settings.database_url
    # Convert sqlite:/// to sqlite+aiosqlite:/// for async support
    if url.startswith("sqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


engine = create_async_engine(get_database_url(), echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Get a database session."""
    async with async_session() as session:
        yield session
