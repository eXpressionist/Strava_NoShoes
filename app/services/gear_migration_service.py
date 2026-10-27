"""One-off Strava gear migration; never imports or creates activities."""

import asyncio
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.services.garmin_service import GarminService
from app.services.strava_service import StravaService


def gear_uuid(item: dict) -> str:
    return str(item.get("uuid") or item.get("gearUUID") or "").lower().replace("-", "")


def gear_type(item: dict) -> str:
    value = item.get("gearType", "")
    return str(value.get("typeKey", "") if isinstance(value, dict) else value).upper()


def marker(source_id: str, athlete_id: int) -> str:
    return f"[noshoes:strava:{athlete_id}:{source_id}]"


def family(sport: str) -> str:
    if sport in {
        "Run",
        "TrailRun",
        "VirtualRun",
        "running",
        "trail_running",
        "treadmill_running",
        "track_running",
        "ultra_run",
    }:
        return "run"
    if sport in {
        "Ride",
        "VirtualRide",
        "MountainBikeRide",
        "GravelRide",
        "EBikeRide",
        "EMountainBikeRide",
        "cycling",
        "mountain_biking",
        "indoor_cycling",
        "road_biking",
        "gravel_cycling",
        "e_bike_fitness",
        "e_bike_mountain",
    }:
        return "ride"
    if sport in {"Walk", "walking"}:
        return "walk"
    if sport in {"Hike", "hiking"}:
        return "hike"
    return ""


@dataclass(frozen=True)
class MigrationActivity:
    id: int
    started: datetime
    family: str
    distance: float
    duration: float
    gear_id: str | None = None
    name: str = ""

    @classmethod
    def parse(cls, raw: dict, source: str) -> "MigrationActivity":
        summary = raw.get("summaryDTO", raw) if source == "garmin" else raw
        timestamp = (
            summary.get("startTimeGMT") if source == "garmin" else raw.get("start_date")
        )
        # Never substitute local time or now for a missing/invalid UTC timestamp.
        started = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        started = (
            started.replace(tzinfo=timezone.utc)
            if started.tzinfo is None
            else started.astimezone(timezone.utc)
        )
        sport = raw.get("activityType") or raw.get("activityTypeDTO") or {}
        sport = (
            sport.get("typeKey", "")
            if source == "garmin"
            else raw.get("sport_type", raw.get("type", ""))
        )
        distance = float(summary.get("distance", -1))
        duration = float(
            summary.get("elapsedDuration", summary.get("duration", -1))
            if source == "garmin"
            else raw.get("elapsed_time", -1)
        )
        if (
            not math.isfinite(distance)
            or not math.isfinite(duration)
            or distance < 0
            or duration <= 0
        ):
            raise ValueError("Invalid activity distance or elapsed duration")
        return cls(
            int(raw["activityId"] if source == "garmin" else raw["id"]),
            started,
            family(sport),
            distance,
            duration,
            raw.get("gear_id") if source == "strava" else None,
            str(raw.get("activityName" if source == "garmin" else "name", "")),
        )


def match_activities(
    sources: list[MigrationActivity], targets: list[MigrationActivity]
) -> dict[int, dict]:
    """Only accept a one-to-one match, even when duplicate uploads look identical."""
    candidates = {
        source.id: [
            target.id
            for target in targets
            if source.family
            and source.family == target.family
            and abs((source.started - target.started).total_seconds()) <= 90
            and abs(source.distance - target.distance)
            <= max(100, source.distance * 0.03)
            and abs(source.duration - target.duration)
            <= max(120, source.duration * 0.05)
        ]
        for source in sources
    }
    uses = Counter(target for choices in candidates.values() for target in choices)
    return {
        source.id: {
            "status": (
                "unsupported_sport"
                if not source.family
                else (
                    "unmatched"
                    if not candidates[source.id]
                    else (
                        "matched"
                        if len(candidates[source.id]) == 1
                        and uses[candidates[source.id][0]] == 1
                        else "ambiguous_activity"
                    )
                )
            ),
            "candidates": candidates[source.id],
        }
        for source in sources
    }


class GearMigration:
    def __init__(
        self,
        strava: StravaService | None = None,
        garmin: GarminService | None = None,
        delay: float = 0.5,
    ):
        self.strava = strava or StravaService()
        self.garmin = garmin or GarminService()
        self.delay = delay

    async def _call(self, method_name: str, *args, **kwargs) -> Any:
        await asyncio.sleep(self.delay)
        return await asyncio.to_thread(
            getattr(self.garmin.client, method_name), *args, **kwargs
        )

    async def _gear_list(self) -> list[dict]:
        result = []
        for start in range(0, 10000, 100):
            page = await self._call(
                "get_gear_list",
                start=start,
                limit=100,
                gear_statuses=["ACTIVE", "RETIRED"],
            )
            if not isinstance(page, list):
                raise ValueError("Unexpected Garmin gear list response")
            result.extend(page)
            if len(page) < 100:
                if any(not gear_uuid(item) for item in result):
                    raise ValueError("Garmin returned gear without a UUID")
                return result
        raise ValueError("Garmin gear pagination limit exceeded")

    async def _activity_gear(self, activity_id: int) -> list[dict]:
        payload = await self._call("get_activity_gear_v2", activity_id)
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) or not gear_uuid(item) for item in payload
        ):
            raise ValueError("Unexpected Garmin activity gear response")
        return payload

    async def build_plan(
        self,
        first: date,
        last: date,
        timezone_name: str = "Europe/Moscow",
        mapping: dict[str, str] | None = None,
    ) -> dict:
        if last < first:
            raise ValueError("End date must not precede start date")
        zone = ZoneInfo(timezone_name)
        lower = datetime.combine(first, time.min, zone).astimezone(timezone.utc)
        upper = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(
            timezone.utc
        )
        await self.garmin._ensure_connected()
        athlete = await self.strava._make_request("GET", "/athlete")
        athlete_id = int(athlete["id"])
        raw_sources = []
        for page in range(1, 1001):
            batch = await self.strava._make_request(
                "GET",
                "/athlete/activities",
                params={
                    "after": int(lower.timestamp()) - 1,
                    "before": int(upper.timestamp()),
                    "page": page,
                    "per_page": 200,
                },
            )
            if not isinstance(batch, list):
                raise ValueError("Unexpected Strava activities response")
            raw_sources.extend(batch)
            if len(batch) < 200:
                break
        else:
            raise ValueError("Strava pagination limit exceeded")
        # Pad Garmin's local date search, then enforce identical UTC bounds below.
        raw_targets = await self._call(
            "get_activities_by_date",
            (first - timedelta(days=1)).isoformat(),
            (last + timedelta(days=1)).isoformat(),
        )
        if not isinstance(raw_targets, list):
            raise ValueError("Unexpected Garmin activities response")
        invalid = []
        parsed = {}
        for source, raw_items in (("strava", raw_sources), ("garmin", raw_targets)):
            items = {}
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("Unexpected activity response")
                try:
                    activity = MigrationActivity.parse(raw, source)
                except (ValueError, TypeError, KeyError, AttributeError):
                    invalid.append(
                        {
                            "source": source,
                            "id": raw.get("id", raw.get("activityId")),
                            "status": "invalid_activity",
                        }
                    )
                    continue
                if lower <= activity.started < upper:
                    if activity.id in items and items[activity.id] != activity:
                        raise ValueError("Conflicting duplicate activity IDs")
                    items[activity.id] = activity
            parsed[source] = list(items.values())
        sources, targets = parsed["strava"], parsed["garmin"]
        matches = match_activities(sources, targets)
        all_gear = await self._gear_list()
        gear_plans = {}
        mapping = mapping or {}
        for source_id in sorted({a.gear_id for a in sources if a.gear_id}):
            source_gear = await self.strava._make_request("GET", f"/gear/{source_id}")
            if str(source_gear.get("id")) != source_id:
                raise ValueError("Strava gear ID mismatch")
            kind = (
                "SHOES"
                if source_id.startswith("g")
                else "BIKE" if source_id.startswith("b") else ""
            )
            tag = marker(source_id, athlete_id)
            candidates = [
                g
                for g in all_gear
                if gear_type(g) == kind and tag in str(g.get("notes", ""))
            ]
            if source_id in mapping:
                target_id = str(mapping[source_id]).lower().replace("-", "")
                candidates = [
                    g
                    for g in all_gear
                    if gear_uuid(g) == target_id and gear_type(g) == kind
                ]
                if len(candidates) != 1:
                    raise ValueError(f"Invalid Garmin gear mapping for {source_id}")
            elif not candidates:
                name = str(source_gear.get("name", "")).strip().casefold()
                candidates = [
                    g
                    for g in all_gear
                    if gear_type(g) == kind
                    and name
                    and str(g.get("name", "")).strip().casefold() == name
                    and all(
                        not source_gear.get(s)
                        or not g.get(t)
                        or str(source_gear[s]).strip().casefold()
                        == str(g[t]).strip().casefold()
                        for s, t in (("brand_name", "brand"), ("model_name", "model"))
                    )
                ]
            first_used = min(
                a.started.astimezone(zone).date()
                for a in sources
                if a.gear_id == source_id
            )
            gear_plans[source_id] = {
                "source_id": source_id,
                "name": source_gear.get("name") or source_id,
                "brand": source_gear.get("brand_name") or "Unknown",
                "model": source_gear.get("model_name")
                or source_gear.get("name")
                or source_id,
                "gear_type": kind,
                "first_use_date": first_used.isoformat(),
                "strava_lifetime_distance_m": source_gear.get("distance"),
                "strava_window_distance_m": sum(
                    a.distance for a in sources if a.gear_id == source_id
                ),
                "notes": tag,
                "target_uuid": (
                    gear_uuid(candidates[0]) if len(candidates) == 1 else None
                ),
                "status": (
                    "unsupported_gear"
                    if not kind
                    else (
                        "ambiguous_gear"
                        if len(candidates) > 1
                        else "existing" if candidates else "create"
                    )
                ),
            }
        claimed = Counter(
            g["target_uuid"] for g in gear_plans.values() if g["target_uuid"]
        )
        for gear in gear_plans.values():
            if gear["target_uuid"] and claimed[gear["target_uuid"]] > 1:
                gear["status"] = "ambiguous_gear"
        links = []
        for activity in sources:
            if not activity.gear_id:
                continue
            gear = gear_plans[activity.gear_id]
            match = matches[activity.id]
            row = {
                "strava_activity_id": activity.id,
                "name": activity.name,
                "started_at": activity.started.isoformat(),
                "source_gear_id": activity.gear_id,
                "garmin_activity_id": None,
                "status": match["status"],
                "candidates": match["candidates"],
            }
            if match["status"] == "matched":
                row["garmin_activity_id"] = match["candidates"][0]
                if gear["status"] in {"unsupported_gear", "ambiguous_gear"}:
                    row["status"] = gear["status"]
                else:
                    current = await self._activity_gear(row["garmin_activity_id"])
                    row["previous_gear"] = current
                    row["status"] = self._link_status(current, gear)
            links.append(row)
        return {
            "version": 1,
            "strava_athlete_id": athlete_id,
            "from_date": first.isoformat(),
            "to_date": last.isoformat(),
            "timezone": timezone_name,
            "garmin_activities": len(targets),
            "strava_activities": len(sources),
            "gear": gear_plans,
            "activities": links,
            "invalid": invalid,
            "summary": dict(Counter(row["status"] for row in links)),
        }

    @staticmethod
    def _link_status(current: list[dict], gear: dict) -> str:
        related = [
            item
            for item in current
            if not gear_type(item) or gear_type(item) == gear["gear_type"]
        ]
        conflicting = [
            item for item in related if gear_uuid(item) != gear["target_uuid"]
        ]
        if conflicting:
            return "gear_conflict"
        if gear["target_uuid"] and any(
            gear_uuid(item) == gear["target_uuid"] for item in current
        ):
            return "already_linked"
        return "link"

    async def apply(
        self,
        plan: dict,
        journal: Callable[[dict], None],
        replace_existing: bool = False,
    ) -> dict:
        result = {
            "created": 0,
            "linked": 0,
            "replaced": 0,
            "already_linked": 0,
            "skipped": 0,
        }
        # Recheck account identity. The CLI supplies a fresh plan from this same
        # invocation; report files are outputs and are never executed as plans.
        if (
            int((await self.strava._make_request("GET", "/athlete"))["id"])
            != plan["strava_athlete_id"]
        ):
            raise ValueError("Strava account changed during migration")
        for row in plan["activities"]:
            if row["status"] == "already_linked":
                result["already_linked"] += 1
                continue
            if row["status"] not in (
                {"link", "gear_conflict"} if replace_existing else {"link"}
            ):
                result["skipped"] += 1
                continue
            gear = plan["gear"][row["source_gear_id"]]
            current = await self._activity_gear(row["garmin_activity_id"])
            status = self._link_status(current, gear)
            if status == "gear_conflict" and not replace_existing:
                result["skipped"] += 1
                continue
            if any(not gear_type(item) for item in current):
                result["skipped"] += 1
                continue
            if not gear["target_uuid"]:
                # Read again before creating; the note marker survives an
                # interrupted run even if create_gear's response was lost.
                existing = [
                    g
                    for g in await self._gear_list()
                    if gear["notes"] in str(g.get("notes", ""))
                    and gear_type(g) == gear["gear_type"]
                ]
                if len(existing) > 1:
                    raise ValueError(
                        "Duplicate migration markers; resolve gear manually"
                    )
                if not existing:
                    journal({"event": "before_create", "gear": dict(gear)})
                    await self._call(
                        "create_gear",
                        gear_type=gear["gear_type"],
                        brand=gear["brand"],
                        model=gear["model"],
                        name=gear["name"],
                        first_use_date=gear["first_use_date"],
                        notes=gear["notes"],
                        activity_type_keys=[],
                    )
                    existing = [
                        g
                        for g in await self._gear_list()
                        if gear["notes"] in str(g.get("notes", ""))
                        and gear_type(g) == gear["gear_type"]
                    ]
                    if len(existing) != 1:
                        raise ValueError(
                            "Created gear could not be verified; rerun the report"
                        )
                    result["created"] += 1
                    journal(
                        {
                            "event": "created",
                            "source_gear_id": gear["source_id"],
                            "target_uuid": gear_uuid(existing[0]),
                        }
                    )
                gear["target_uuid"] = gear_uuid(existing[0])
            target = gear["target_uuid"]
            before_ids = {gear_uuid(item) for item in current}
            journal(
                {
                    "event": "before_link",
                    "garmin_activity_id": row["garmin_activity_id"],
                    "source_gear_id": gear["source_id"],
                    "target_uuid": target,
                    "previous_gear": current,
                }
            )
            if target not in before_ids:
                await self._call(
                    "add_gear_to_activity", target, row["garmin_activity_id"]
                )
                if target not in {
                    gear_uuid(item)
                    for item in await self._activity_gear(row["garmin_activity_id"])
                }:
                    raise ValueError("Garmin did not confirm gear assignment; stopping")
                result["linked"] += 1
            # Attach and verify first; only then remove other items of the SAME
            # category when explicitly requested. Bike components are preserved.
            remove = [
                item
                for item in current
                if gear_type(item) == gear["gear_type"] and gear_uuid(item) != target
            ]
            for item in remove:
                await self._call(
                    "remove_gear_from_activity",
                    gear_uuid(item),
                    row["garmin_activity_id"],
                )
            after = {
                gear_uuid(item)
                for item in await self._activity_gear(row["garmin_activity_id"])
            }
            removed = {gear_uuid(item) for item in remove}
            if (
                target not in after
                or removed & after
                or not (before_ids - removed).issubset(after)
            ):
                raise ValueError("Garmin gear verification failed; inspect the journal")
            result["replaced"] += int(bool(remove))
            journal(
                {
                    "event": "linked",
                    "garmin_activity_id": row["garmin_activity_id"],
                    "target_uuid": target,
                    "removed": sorted(removed),
                }
            )
        return result
