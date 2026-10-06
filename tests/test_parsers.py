"""Exercise real input formats, malformed geometry, and upload boundaries."""

import stat
from io import BytesIO
from zipfile import ZipFile, ZipInfo

import pytest
import shapefile
from pyproj import CRS

from app import parsers
from app.parsers import ParseError, parse_file


def kml(geometry: str, properties: str = "") -> bytes:
    return (
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f'<Placemark id="parcel-1">{properties}{geometry}</Placemark>'
        "</Document></kml>"
    ).encode()


def archive(members: dict[str, bytes]) -> bytes:
    result = BytesIO()
    with ZipFile(result, "w") as zipped:
        for name, value in members.items():
            zipped.writestr(name, value)
    return result.getvalue()


def shape_members() -> dict[str, bytes]:
    shp, shx, dbf = BytesIO(), BytesIO(), BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYLINE) as writer:
        writer.field("owner", "C")
        writer.line([[[0, 0], [3, 4]]])
        writer.record("Ada")
    return {
        "survey.SHP": shp.getvalue(),
        "survey.SHX": shx.getvalue(),
        "survey.DBF": dbf.getvalue(),
        "survey.PRJ": CRS.from_epsg(3857).to_wkt().encode(),
    }


def test_reads_nested_shapefile_and_retains_source_crs():
    dataset = parse_file(
        "survey.ZIP", archive({"data/" + name: value for name, value in shape_members().items()})
    )
    assert dataset.crs.to_epsg() == 3857
    assert dataset.features[0].geometry.length == 5
    assert dataset.features[0].properties == {"owner": "Ada"}


def test_shapefile_requires_declared_crs():
    members = shape_members()
    del members["survey.PRJ"]
    with pytest.raises(ParseError, match=r"\.prj"):
        parse_file("survey.zip", archive(members))


def test_shapefile_rejects_corrupt_record():
    members = shape_members()
    members["survey.SHP"] = members["survey.SHP"][:110]
    with pytest.raises(ParseError, match="malformed"):
        parse_file("survey.zip", archive(members))


def test_polygon_holes_and_kml_metadata():
    dataset = parse_file(
        "parcel.kml",
        kml(
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            "0,0 1,0 1,1 0,1 0,0"
            "</coordinates></LinearRing></outerBoundaryIs>"
            "<innerBoundaryIs><LinearRing><coordinates>"
            "0.25,0.25 0.25,0.75 0.75,0.75 0.75,0.25 0.25,0.25"
            "</coordinates></LinearRing></innerBoundaryIs></Polygon>",
            "<name>Parcel 1</name><description><![CDATA[<b>Surveyed</b>]]></description>"
            '<ExtendedData><Data name="owner"><value>Ada</value></Data>'
            '<SchemaData><SimpleData name="zone">A</SimpleData></SchemaData></ExtendedData>',
        ),
    )
    feature = dataset.features[0]
    assert feature.feature_id == "parcel-1"
    assert feature.geometry.area == pytest.approx(0.75)
    assert len(feature.geometry.interiors) == 1
    assert feature.properties == {
        "name": "Parcel 1",
        "description": "<b>Surveyed</b>",
        "owner": "Ada",
        "zone": "A",
    }
    assert dataset.crs.to_epsg() == 4326


def test_mixed_geometry_preserves_all_supported_parts():
    result = parse_file(
        "mixed.kml",
        kml(
            "<MultiGeometry><Point><coordinates>1,2,100</coordinates></Point>"
            "<LineString><coordinates>0,0 1,1</coordinates></LineString></MultiGeometry>"
        ),
    )
    geometry = result.features[0].geometry
    assert geometry.geom_type == "GeometryCollection"
    assert [part.geom_type for part in geometry.geoms] == ["Point", "LineString"]
    assert not geometry.has_z


def test_unsupported_geometry_has_explicit_reason():
    result = parse_file("model.kml", kml("<Model><altitudeMode>absolute</altitudeMode></Model>"))
    assert result.features[0].geometry is None
    assert result.features[0].unsupported_geometry == "Model"


def test_unsupported_geometry_cannot_be_forged_by_user_properties():
    result = parse_file(
        "line.kml",
        kml(
            "<LineString><coordinates>0,0 1,1</coordinates></LineString>",
            '<ExtendedData><Data name="_unsupported_geometry"><value>Model</value></Data></ExtendedData>',
        ),
    )
    assert result.features[0].unsupported_geometry is None
    assert result.features[0].properties["_unsupported_geometry"] == "Model"
    assert result.features[0].geometry.geom_type == "LineString"


def test_standalone_ring_has_valid_geojson_type():
    result = parse_file(
        "ring.kml", kml("<LinearRing><coordinates>0,0 1,0 1,1 0,0</coordinates></LinearRing>")
    )
    assert result.features[0].geometry.__geo_interface__["type"] == "LineString"


def test_unsupported_component_is_reported_in_mixed_geometry():
    result = parse_file(
        "mixed.kml",
        kml(
            "<MultiGeometry><LineString><coordinates>0,0 1,1</coordinates></LineString><Model/></MultiGeometry>"
        ),
    )
    assert result.features[0].unsupported_geometry == "Model"


def test_shapefile_rejects_mismatched_record_counts():
    members = shape_members()
    dbf = bytearray(members["survey.DBF"])
    dbf[4:8] = (0).to_bytes(4, "little")
    members["survey.DBF"] = bytes(dbf)
    with pytest.raises(ParseError, match="record counts"):
        parse_file("survey.zip", archive(members))


@pytest.mark.parametrize(
    "coordinates", ["NaN,1", "1,inf", "181,0", "0,-91", "1,2,inf", "one,two", "1,2,", "1,2,3,4"]
)
def test_bad_kml_coordinates_are_rejected(coordinates):
    with pytest.raises(ParseError):
        parse_file("invalid.kml", kml(f"<Point><coordinates>{coordinates}</coordinates></Point>"))


@pytest.mark.parametrize(
    "geometry",
    [
        "<Point><coordinates>1,2 3,4</coordinates></Point>",
        "<LineString><coordinates>1,2</coordinates></LineString>",
        "<LinearRing><coordinates>0,0 1,0 1,1 0,1</coordinates></LinearRing>",
        "<Point></Point>",
    ],
)
def test_invalid_geometry_structure_is_rejected(geometry):
    with pytest.raises(ParseError):
        parse_file("invalid.kml", kml(geometry))


@pytest.mark.parametrize(
    "name",
    [
        "../survey.shp",
        "/survey.shp",
        "data/../survey.shp",
        r"data\survey.shp",
        "data/./survey.shp",
        "C:/survey.shp",
    ],
)
def test_archive_rejects_ambiguous_or_escaping_paths(name):
    with pytest.raises(ParseError, match="path"):
        parse_file("bad.zip", archive({name: b"data"}))


def test_archive_rejects_case_insensitive_duplicates():
    with pytest.raises(ParseError, match="duplicate"):
        parse_file("bad.zip", archive({"data.shp": b"data", "DATA.SHP": b"data"}))


def test_archive_rejects_symlinks():
    result = BytesIO()
    info = ZipInfo("data.shp")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(result, "w") as zipped:
        zipped.writestr(info, "/some/target")
    with pytest.raises(ParseError, match="symbolic"):
        parse_file("bad.zip", result.getvalue())


def test_prohibits_xml_entities():
    malicious = b'<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]><kml><Placemark><name>&x;</name></Placemark></kml>'
    with pytest.raises(ParseError, match="DTD/entity"):
        parse_file("evil.kml", malicious)


def test_feature_and_coordinate_limits(monkeypatch):
    with pytest.raises(ParseError, match="feature limit"):
        parse_file("many.kml", b"<kml><Placemark/><Placemark/></kml>", max_features=1)
    monkeypatch.setattr(parsers, "MAX_COORDINATES", 2)
    with pytest.raises(ParseError, match="coordinate limit"):
        parse_file(
            "many.kml", kml("<LineString><coordinates>0,0 1,1 2,2</coordinates></LineString>")
        )


def test_rejects_deep_xml():
    content = ("<kml>" + "<Folder>" * 65 + "<Placemark/>" + "</Folder>" * 65 + "</kml>").encode()
    with pytest.raises(ParseError, match="depth"):
        parse_file("deep.kml", content)
