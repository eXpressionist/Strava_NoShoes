"""Read the existing Strava backup without credentials or Strava API calls."""

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.database import ActivityDB, GearDB, async_session


class StravaArchive:
    async def identity(self) -> int | None:
        """Recover account identity when it is present in saved original JSON."""
        async with async_session() as session:
            payloads = await session.scalars(
                select(ActivityDB.raw_data).where(ActivityDB.source == "strava")
            )
            identities = set()
            for payload in payloads:
                try:
                    identity = json.loads(payload)["athlete"]["id"]
                    if int(identity) > 0:
                        identities.add(int(identity))
                except (ValueError, TypeError, KeyError):
                    continue
        if len(identities) > 1:
            raise ValueError("Strava archive contains multiple athletes")
        return next(iter(identities), None)

    async def coverage(self) -> dict:
        try:
            async with async_session() as session:
                count, first, last = (
                    await session.execute(
                        select(
                            func.count(ActivityDB.id),
                            func.min(ActivityDB.start_date),
                            func.max(ActivityDB.start_date),
                        ).where(ActivityDB.source == "strava")
                    )
                ).one()
        except SQLAlchemyError as exc:
            raise ValueError(
                "Strava SQLite backup unavailable; check DATABASE_URL and the data volume"
            ) from exc
        if not count:
            raise ValueError("SQLite backup contains no Strava activities")
        return {
            "source": "sqlite",
            "activities": count,
            "first_activity_utc": first.isoformat(),
            "latest_activity_utc": last.isoformat(),
        }

    async def activities(self, lower: datetime, upper: datetime) -> list[dict]:
        # Existing backup timestamps are naive UTC. Database columns are the
        # authoritative values; original JSON can be missing or out of date.
        async with async_session() as session:
            rows = await session.scalars(
                select(ActivityDB)
                .where(
                    ActivityDB.source == "strava",
                    ActivityDB.start_date >= lower.replace(tzinfo=None),
                    ActivityDB.start_date < upper.replace(tzinfo=None),
                )
                .order_by(ActivityDB.start_date)
            )
            return [
                {
                    "id": row.source_id,
                    "name": row.name,
                    "start_date": row.start_date.isoformat(),
                    "sport_type": row.sport_type,
                    "distance": row.distance,
                    "elapsed_time": row.elapsed_time,
                    "gear_id": row.gear_id,
                }
                for row in rows
            ]

    async def gear(self, source_id: str) -> dict:
        async with async_session() as session:
            row = await session.scalar(
                select(GearDB).where(
                    GearDB.source == "strava", GearDB.source_id == source_id
                )
            )
            if row is not None:
                return {
                    "id": row.source_id,
                    "name": row.name,
                    "brand_name": row.brand,
                    "model_name": row.model,
                    "distance": row.distance,
                }
            # Older backups may have the activity's shoe name but no Gear row.
            name = await session.scalar(
                select(ActivityDB.gear_name)
                .where(
                    ActivityDB.source == "strava",
                    ActivityDB.gear_id == source_id,
                    ActivityDB.gear_name.is_not(None),
                    ActivityDB.gear_name != "",
                )
                .order_by(ActivityDB.start_date.desc())
                .limit(1)
            )
        return {"id": source_id, "name": name}
