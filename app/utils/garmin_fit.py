"""Convert official Garmin activity downloads to GPX without extracting ZIPs."""

import io
import math
import zipfile
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement

from app.services.garmin_api import GarminAPIError

MAX_FIT_SIZE = 64 * 1024 * 1024


def activity_file_to_gpx(content: bytes, name: str) -> str:
    # These optional/expensive imports are needed only when exporting a track.
    import gpxpy.gpx
    from garmin_fit_sdk import Decoder, Stream

    try:
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                files = [
                    item
                    for item in archive.infolist()
                    if item.filename.lower().endswith(".fit")
                ]
                if len(files) != 1 or files[0].file_size > MAX_FIT_SIZE:
                    raise GarminAPIError(
                        "Garmin download must contain one FIT activity"
                    )
                content = archive.read(files[0])
        if len(content) > MAX_FIT_SIZE:
            raise GarminAPIError("Garmin activity file is too large")
        messages, errors = Decoder(Stream.from_byte_array(bytearray(content))).read()
        if errors:
            raise GarminAPIError("Unable to decode Garmin FIT activity")
        gpx = gpxpy.gpx.GPX()
        gpx.creator = "Strava NoShoes"
        gpx.nsmap["gpxtpx"] = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
        track = gpxpy.gpx.GPXTrack(name=name)
        gpx.tracks.append(track)
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        for record in messages.get("record_mesgs", []):
            raw_lat, raw_lon = record.get("position_lat"), record.get("position_long")
            if raw_lat is None or raw_lon is None:
                continue
            lat, lon = raw_lat * 180 / 2**31, raw_lon * 180 / 2**31
            if not (
                math.isfinite(lat)
                and math.isfinite(lon)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                continue
            altitude = record.get("enhanced_altitude")
            if altitude is None:
                altitude = record.get("altitude")
            timestamp = record.get("timestamp")
            point = gpxpy.gpx.GPXTrackPoint(
                lat,
                lon,
                elevation=altitude,
                time=timestamp if isinstance(timestamp, datetime) else None,
            )
            extension = Element(
                "{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}TrackPointExtension"
            )
            for tag, value in (
                ("hr", record.get("heart_rate")),
                ("cad", record.get("cadence")),
            ):
                if value is not None:
                    SubElement(
                        extension,
                        f"{{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}}{tag}",
                    ).text = str(value)
            if len(extension):
                point.extensions.append(extension)
            segment.points.append(point)
        if not segment.points:
            raise GarminAPIError("Garmin activity has no GPS track")
        return gpx.to_xml()
    except GarminAPIError:
        raise
    except Exception:
        raise GarminAPIError("Unable to decode Garmin activity download") from None
