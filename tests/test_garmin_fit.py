"""Exercise the real Garmin FIT decoder with a small, synthetic GPS activity."""

import io
import struct
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import gpxpy
import pytest

from app.services.garmin_api import GarminAPIError
from app.services.garmin_service import GarminService
from app.utils.garmin_fit import activity_file_to_gpx


def fit_activity(latitude=2**29):
    fields = bytes([253, 4, 0x86, 0, 4, 0x85, 1, 4, 0x85, 2, 2, 0x84, 3, 1, 2, 4, 1, 2])
    data = b"\x40" + struct.pack("<BBHB", 0, 0, 20, 6) + fields
    data += b"\x00" + struct.pack(
        "<IiiHBB", 1_000_000_000, latitude, 2**28, 3000, 150, 80
    )
    content = struct.pack("<BBHI4s", 12, 0x20, 2100, len(data), b".FIT") + data
    crc = 0
    for byte in content:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return content + struct.pack("<H", crc)


@pytest.mark.parametrize("zipped", [False, True])
def test_fit_export_preserves_coordinates_elevation_time_and_sensors(zipped):
    content = fit_activity()
    if zipped:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("activity.fit", content)
        content = buffer.getvalue()
    gpx = gpxpy.parse(activity_file_to_gpx(content, "Morning run"))
    point = gpx.tracks[0].segments[0].points[0]
    assert point.latitude == 45
    assert point.longitude == 22.5
    assert point.elevation == 100
    assert point.time is not None
    assert [child.text for child in point.extensions[0]] == ["150", "80"]


def test_export_rejects_corrupt_fit_and_activity_without_gps():
    with pytest.raises(GarminAPIError, match="decode"):
        activity_file_to_gpx(b"not a FIT file", "Run")
    with pytest.raises(GarminAPIError, match="no GPS"):
        activity_file_to_gpx(fit_activity(latitude=0x7FFFFFFF), "Indoor run")


@pytest.mark.asyncio
async def test_service_downloads_original_file_and_caches_gpx(tmp_path):
    service = GarminService()
    service.client = AsyncMock()
    service.client.download_activity.return_value = fit_activity()
    path = await service.download_gpx(123, str(tmp_path), "Morning run")
    assert len(gpxpy.parse(Path(path).read_text(encoding="utf-8")).tracks) == 1
    assert await service.download_gpx(123, str(tmp_path), "Morning run") == path
    service.client.download_activity.assert_awaited_once_with(123)
