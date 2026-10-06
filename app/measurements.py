"""Calculate horizontal measurements in a suitable local metric projection.

The API deliberately supports local features, not global measurements. Features
must span at most six degrees in longitude and latitude. UTM is used between
80°S and 84°N, with every vertex within six degrees of the selected central
meridian. Entirely polar features use UPS. Antimeridian-crossing features and
features crossing the UTM/polar transition are reported as unsupported. These
are projected, planar measurements; projection distortion remains possible.
Altitude and measure ordinates are ignored and output GeoJSON contains XY only.
"""

from __future__ import annotations

import json
import math
from typing import Any

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import force_2d, get_coordinates
from shapely.errors import GEOSException
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import explain_validity

WGS84 = CRS.from_epsg(4326)
MAX_SPAN_DEGREES = 6.0
_AREA_TYPES = {"Polygon", "MultiPolygon"}
_LENGTH_TYPES = {"LineString", "MultiLineString"}


def _result(
    geometry_type: str | None,
    status: str,
    reason: str | None,
    *,
    geometry: dict[str, Any] | None = None,
    area_m2: float | None = None,
    length_m: float | None = None,
    crs: str | None = None,
) -> dict[str, Any]:
    return {
        "geometry": geometry,
        "geometry_type": geometry_type,
        "measurement": {
            "status": status,
            "area_m2": area_m2,
            "length_m": length_m,
            "crs": crs,
            "reason": reason,
        },
    }


def _finite(geometry: BaseGeometry) -> bool:
    return all(
        math.isfinite(value) for coordinate in get_coordinates(geometry) for value in coordinate
    )


def _metric_crs(geometry: BaseGeometry) -> tuple[CRS | None, str | None]:
    west, south, east, north = geometry.bounds
    if east - west > MAX_SPAN_DEGREES or north - south > MAX_SPAN_DEGREES:
        return None, (
            "Measurements require a local feature spanning at most 6 degrees in "
            "longitude and latitude; large or antimeridian-crossing features are unsupported."
        )
    if south >= 84:
        return CRS.from_epsg(32661), None
    if north <= -80:
        return CRS.from_epsg(32761), None
    if south < -80 or north > 84:
        return None, "Features crossing the UTM/polar latitude boundary are unsupported."
    longitude = (west + east) / 2
    latitude = (south + north) / 2
    zone = min(60, max(1, math.floor((longitude + 180) / 6) + 1))
    central_meridian = zone * 6 - 183
    if max(abs(west - central_meridian), abs(east - central_meridian)) > 6:
        return None, "Feature extends too far from its local UTM central meridian."
    return CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone), None


def measure_feature(geometry: BaseGeometry | None, source_crs: CRS) -> dict[str, Any]:
    """Return JSON-safe WGS84 geometry and one per-feature measurement outcome.

    The input is never modified. Missing, empty, malformed, or non-finite
    geometries return INVALID. Unsupported but valid geometries retain their
    canonical geometry. Failures are isolated to this feature.
    """
    geometry_type = geometry.geom_type if isinstance(geometry, BaseGeometry) else None
    if geometry is None:
        return _result(None, "INVALID", "Feature has no geometry.")
    if not isinstance(geometry, BaseGeometry):
        return _result(None, "INVALID", "Feature geometry is not a recognized geometry.")

    canonical: dict[str, Any] | None = None
    try:
        if geometry.is_empty:
            return _result(geometry_type, "INVALID", "Feature geometry is empty.")
        horizontal = force_2d(geometry)
        if not _finite(horizontal):
            return _result(
                geometry_type, "INVALID", "Geometry contains non-finite horizontal coordinates."
            )
        if not horizontal.is_valid:
            return _result(
                geometry_type, "INVALID", f"Invalid geometry: {explain_validity(horizontal)}"
            )
        if geometry_type == "LinearRing":
            return _result(
                geometry_type,
                "UNSUPPORTED",
                "A standalone LinearRing is not a GeoJSON geometry type.",
            )

        horizontal_crs = CRS.from_user_input(source_crs).to_2d()
        if (
            not (horizontal_crs.is_geographic or horizontal_crs.is_projected)
            or len(horizontal_crs.axis_info) != 2
        ):
            return _result(
                geometry_type,
                "INVALID",
                "A geographic or projected horizontal source CRS is required.",
            )
        to_wgs84 = Transformer.from_crs(horizontal_crs, WGS84, always_xy=True)
        geographic = transform(lambda x, y: to_wgs84.transform(x, y, errcheck=True), horizontal)
        if not _finite(geographic):
            return _result(
                geometry_type, "INVALID", "CRS transformation produced non-finite coordinates."
            )
        west, south, east, north = geographic.bounds
        if west < -180 or east > 180 or south < -90 or north > 90:
            return _result(
                geometry_type,
                "INVALID",
                "Coordinates are outside the valid WGS84 longitude/latitude range.",
            )
        if not geographic.is_valid:
            return _result(
                geometry_type,
                "INVALID",
                f"Invalid transformed geometry: {explain_validity(geographic)}",
            )
        canonical = json.loads(json.dumps(mapping(geographic), allow_nan=False))

        if geometry_type in {"Point", "MultiPoint"}:
            return _result(
                geometry_type,
                "NOT_APPLICABLE",
                "Point geometries have no area or line length.",
                geometry=canonical,
            )
        if geometry_type not in _AREA_TYPES | _LENGTH_TYPES:
            return _result(
                geometry_type,
                "UNSUPPORTED",
                "This geometry type is not supported for measurement.",
                geometry=canonical,
            )

        target_crs, reason = _metric_crs(geographic)
        if target_crs is None:
            return _result(geometry_type, "UNSUPPORTED", reason, geometry=canonical)
        projector = Transformer.from_crs(WGS84, target_crs, always_xy=True)
        projected = transform(lambda x, y: projector.transform(x, y, errcheck=True), geographic)
        if not _finite(projected) or not projected.is_valid:
            return _result(
                geometry_type,
                "INVALID",
                "Geometry is invalid after metric projection.",
                geometry=canonical,
            )
        area_m2 = float(projected.area) if geometry_type in _AREA_TYPES else None
        length_m = float(projected.length) if geometry_type in _LENGTH_TYPES else None
        measurement = area_m2 if area_m2 is not None else length_m
        if measurement is None or not math.isfinite(measurement) or measurement <= 0:
            return _result(
                geometry_type,
                "INVALID",
                "Projection produced a non-positive or non-finite measurement.",
                geometry=canonical,
            )
        return _result(
            geometry_type,
            "CALCULATED",
            None,
            geometry=canonical,
            area_m2=area_m2,
            length_m=length_m,
            crs=target_crs.to_string(),
        )
    except (ProjError, GEOSException, ValueError, TypeError, OverflowError) as exc:
        return _result(
            geometry_type,
            "INVALID",
            f"Geometry or CRS could not be processed: {exc}",
            geometry=canonical,
        )
