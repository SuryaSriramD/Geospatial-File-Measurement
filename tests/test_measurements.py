"""Metric assertions use independently known metre-based source geometries."""

import json
import math

import pytest
from pyproj import CRS, Transformer
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.ops import transform

from app.measurements import measure_feature

WGS84 = CRS.from_epsg(4326)
UTM = CRS.from_epsg(32631)


def as_wgs84(geometry):
    return transform(Transformer.from_crs(UTM, WGS84, always_xy=True).transform, geometry)


def square(x=500000, y=1000000, size=100):
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def test_geographic_line_known_distance_and_xy_axis_order():
    # On the equator, longitude 3° is the zone 31 central meridian.
    result = measure_feature(LineString([(3, 0), (3, 0.01)]), WGS84)
    assert result["measurement"]["status"] == "CALCULATED"
    assert result["measurement"]["length_m"] == pytest.approx(1105.30, abs=0.02)
    assert result["measurement"]["crs"] == "EPSG:32631"
    assert result["measurement"]["area_m2"] is None
    assert result["geometry"]["coordinates"] == [[3.0, 0.0], [3.0, 0.01]]


@pytest.mark.parametrize("source_crs", [WGS84, UTM])
def test_square_area_in_geographic_and_projected_sources(source_crs):
    polygon = square()
    geometry = as_wgs84(polygon) if source_crs == WGS84 else polygon
    result = measure_feature(geometry, source_crs)
    assert result["measurement"]["status"] == "CALCULATED"
    assert result["measurement"]["area_m2"] == pytest.approx(10000, abs=0.001)
    assert result["measurement"]["length_m"] is None
    assert result["measurement"]["crs"] == "EPSG:32631"
    assert -180 <= result["geometry"]["coordinates"][0][0][0] <= 180


def test_holes_and_multipart_area():
    outer = square()
    hole = square(x=500010, y=1000010, size=20)
    with_hole = Polygon(outer.exterior.coords, [hole.exterior.coords])
    geometry = MultiPolygon([with_hole, square(x=500200, size=50)])
    result = measure_feature(geometry, UTM)
    assert result["geometry_type"] == "MultiPolygon"
    assert result["measurement"]["area_m2"] == pytest.approx(10000 - 400 + 2500, abs=0.001)


def test_multipart_length():
    geometry = MultiLineString(
        [
            [(500000, 1000000), (500003, 1000004)],
            [(500020, 1000000), (500020, 1000012)],
        ]
    )
    result = measure_feature(geometry, UTM)
    assert result["measurement"]["length_m"] == pytest.approx(17, abs=0.00001)


@pytest.mark.parametrize("geometry", [Point(3, 1), MultiPoint([(3, 1), (4, 2)])])
def test_points_are_not_applicable(geometry):
    result = measure_feature(geometry, WGS84)
    assert result["measurement"]["status"] == "NOT_APPLICABLE"
    assert result["measurement"]["area_m2"] is None
    assert result["measurement"]["length_m"] is None
    assert result["geometry"] is not None


def test_collection_unsupported():
    result = measure_feature(GeometryCollection([Point(3, 1), LineString([(3, 1), (3, 2)])]), WGS84)
    assert result["measurement"]["status"] == "UNSUPPORTED"
    assert result["geometry"]["type"] == "GeometryCollection"


def test_standalone_ring_is_unsupported_without_invalid_geojson():
    result = measure_feature(LinearRing([(3, 1), (3.01, 1), (3, 1.01)]), WGS84)
    assert result["measurement"]["status"] == "UNSUPPORTED"
    assert result["geometry"] is None


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        Point(),
        Polygon(),
        LineString(),
        Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]),
        Point(math.inf, 0),
        Point(0, math.nan),
        Point(0, 91),
        Point(181, 0),
        "not a geometry",
    ],
)
def test_invalid_inputs_are_isolated_and_json_safe(geometry):
    result = measure_feature(geometry, WGS84)
    assert result["measurement"]["status"] == "INVALID"
    assert result["measurement"]["reason"]
    assert result["measurement"]["area_m2"] is None
    assert result["measurement"]["length_m"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "geometry",
    [
        LineString([(179.9, 0), (-179.9, 0)]),
        LineString([(0, 0), (7, 0)]),
        LineString([(3, 0), (3, 7)]),
        LineString([(3, 83.9), (3, 84.1)]),
    ],
)
def test_large_antimeridian_and_transition_features_are_unsupported(geometry):
    result = measure_feature(geometry, WGS84)
    assert result["measurement"]["status"] == "UNSUPPORTED"
    assert result["measurement"]["reason"]
    assert result["geometry"] is not None


@pytest.mark.parametrize(
    "coordinates, expected_crs",
    [
        ([(3, 85), (3.1, 85)], "EPSG:32661"),
        ([(3, -85), (3.1, -85)], "EPSG:32761"),
        ([(3, -1), (3.1, -1)], "EPSG:32731"),
        ([(-0.001, 1), (0.001, 1)], "EPSG:32631"),
    ],
)
def test_polar_southern_and_zone_boundary_projections(coordinates, expected_crs):
    result = measure_feature(LineString(coordinates), WGS84)
    assert result["measurement"]["status"] == "CALCULATED"
    assert result["measurement"]["crs"] == expected_crs
    assert math.isfinite(result["measurement"]["length_m"])
    json.dumps(result, allow_nan=False)


def test_altitude_ignored_input_not_mutated():
    geometry = LineString([(3, 0, 100), (3, 0.01, 900)])
    result = measure_feature(geometry, CRS.from_epsg(4979))
    assert geometry.has_z
    assert list(geometry.coords) == [(3, 0, 100), (3, 0.01, 900)]
    assert result["geometry"]["coordinates"] == [[3.0, 0.0], [3.0, 0.01]]
    assert result["measurement"]["length_m"] == pytest.approx(1105.30, abs=0.02)


def test_non_horizontal_crs_invalid():
    result = measure_feature(Point(3, 1), CRS.from_epsg(4978))
    assert result["measurement"]["status"] == "INVALID"


def test_invalid_crs_does_not_raise():
    result = measure_feature(Point(3, 1), "not-a-crs")
    assert result["measurement"]["status"] == "INVALID"
