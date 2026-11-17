"""Async client for the official, read-only Garmin Connect Personal API."""

import asyncio
import base64
import json
import math
import time
from datetime import date
from typing import Any

import httpx
from pydantic import SecretStr

BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"


class GarminAPIError(Exception):
    """Public error containing no response bodies, keys, or bearer tokens."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GarminPersonalClient:
    """Exchange an API key for a short-lived bearer token, kept only in memory."""

    supports_gear = False

    def __init__(
        self, api_key: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._access_token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def _token(self, client: httpx.AsyncClient, rejected: str = "") -> str:
        async with self._token_lock:
            if (
                self._access_token
                and self._access_token != rejected
                and self._expires_at > time.time() + 60
            ):
                return self._access_token
            response = await client.post(
                TOKEN_PATH,
                headers={
                    "Authorization": f"ApiKey {self._api_key.get_secret_value()}",
                    "Accept": "*/*",
                },
            )
            self._check_status(response)
            token = response.text.strip()
            try:
                # Decode only to schedule renewal; Garmin validates the token.
                payload = token.split(".")[1]
                claims = json.loads(
                    base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                )
                expires_at = float(claims["exp"])
                if not math.isfinite(expires_at) or expires_at <= time.time():
                    raise ValueError
            except (ValueError, TypeError, KeyError, IndexError):
                raise GarminAPIError(
                    "Garmin returned an invalid access token"
                ) from None
            self._access_token = token
            self._expires_at = min(expires_at, time.time() + 86400)
            return token

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        if not response.is_success:
            raise GarminAPIError(
                f"Garmin Personal API returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    async def _get(
        self, path: str, *, params: dict | None = None, binary: bool = False
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=30,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                token = await self._token(client)
                for attempt in range(2):
                    response = await client.get(
                        path,
                        params=params,
                        headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
                    )
                    if response.status_code == 401 and attempt == 0:
                        token = await self._token(client, rejected=token)
                        continue
                    self._check_status(response)
                    if binary:
                        if not response.content:
                            raise GarminAPIError(
                                "Garmin returned an empty activity file"
                            )
                        return response.content
                    if response.status_code == 204 or not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        raise GarminAPIError("Garmin returned invalid JSON") from None
        except httpx.RequestError:
            raise GarminAPIError("Unable to connect to Garmin Personal API") from None

    async def get_activities(
        self, start: int = 0, limit: int = 20, **dates: str
    ) -> list[dict]:
        payload = await self._get(
            "/activitylist-service/activities/search/activities",
            params={"start": start, "limit": limit, **dates},
        )
        if not isinstance(payload, list) or any(
            not isinstance(item, dict) for item in payload
        ):
            raise GarminAPIError("Garmin returned an invalid activity list")
        return payload

    async def get_activities_by_date(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        result: dict[int, dict] = {}
        for start in range(0, 100000, 100):
            page = await self.get_activities(
                start, 100, startDate=start_date, endDate=end_date
            )
            previous = len(result)
            for item in page:
                if not isinstance(item.get("activityId"), int):
                    raise GarminAPIError("Garmin returned an activity without an ID")
                result[item["activityId"]] = item
            if len(page) < 100:
                return list(result.values())
            if len(result) == previous:
                raise GarminAPIError("Garmin activity pagination did not advance")
        raise GarminAPIError("Garmin activity pagination limit exceeded")

    async def get_activity(self, activity_id: int) -> dict:
        payload = await self._get(f"/activity-service/activity/{int(activity_id)}")
        if not isinstance(payload, dict):
            raise GarminAPIError("Garmin returned invalid activity details")
        return payload

    async def download_activity(self, activity_id: int) -> bytes:
        content = await self._get(
            f"/download-service/files/activity/{int(activity_id)}", binary=True
        )
        if not isinstance(content, bytes):
            raise GarminAPIError("Garmin returned an invalid activity file")
        return content

    async def get_health(self, metric: str, day: str) -> Any:
        day = date.fromisoformat(day).isoformat()
        endpoints: dict[str, tuple[str, dict]] = {
            "daily_summary": (
                "/usersummary-service/usersummary/daily",
                {"calendarDate": day},
            ),
            "sleep": ("/sleep-service/sleep/dailySleep", {"date": day}),
            "hrv": (f"/hrv-service/hrv/{day}", {}),
            "body_battery": (
                "/wellness-service/wellness/bodyBattery/reports/daily",
                {"startDate": day, "endDate": day},
            ),
            "stress": (f"/usersummary-service/stats/stress/daily/{day}/{day}", {}),
            "heart_rate": ("/wellness-service/wellness/dailyHeartRate", {"date": day}),
            "steps": (f"/usersummary-service/stats/steps/daily/{day}/{day}", {}),
            "spo2": (f"/wellness-service/wellness/daily/spo2acclimation/{day}", {}),
            "body_composition": (
                f"/weight-service/weight/range/{day}/{day}",
                {"includeAll": "true"},
            ),
            "hydration": (
                f"/usersummary-service/stats/hydration/daily/{day}/{day}",
                {},
            ),
        }
        path, params = endpoints[metric]
        return await self._get(path, params=params)
