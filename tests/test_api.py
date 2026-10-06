"""End-to-end REST checks, using files generated in memory."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS, Transformer

from app.config import Settings
from app.main import create_app


def kml(placemarks: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{placemarks}</Document></kml>"
    ).encode("utf-8")


MIXED_KML = kml(
    """
    <Placemark><name>Field</name><ExtendedData>
      <Data name="owner"><value>Ada</value></Data>
    </ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
      0,0 0.001,0 0.001,0.001 0,0.001 0,0
    </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
    <Placemark><name>Road</name><LineString><coordinates>
      0,0 0.001,0
    </coordinates></LineString></Placemark>
    <Placemark><name>Marker</name><Point><coordinates>
      0.0005,0.0005
    </coordinates></Point></Placemark>
    """
)


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        yield api


def upload(client: TestClient, contents: bytes = MIXED_KML, filename="survey.kml"):
    return client.post(
        "/api/files/",
        files={"file": (filename, contents, "application/octet-stream")},
    )


def measurements(client: TestClient, file_id: str, **params):
    return client.get(f"/api/files/{file_id}/measurements/", params=params)


def shapefile_zip(*, include_prj: bool = True, crs: str = "EPSG:3857") -> bytes:
    """A projected square, with a DBF attribute and a known WGS84 footprint."""
    transform = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    # Shapefile exterior rings are clockwise.
    geographic_ring = [(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0), (0, 0)]
    projected_ring = [transform.transform(*point) for point in geographic_ring]
    shp, shx, dbf = BytesIO(), BytesIO(), BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as writer:
        writer.field("owner", "C", size=40)
        writer.poly([projected_ring])
        writer.record("Ada")
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zf:
        for suffix, content in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            zf.writestr(f"survey.{suffix}", content.getvalue())
        if include_prj:
            zf.writestr("survey.prj", CRS.from_user_input(crs).to_wkt())
    return archive.getvalue()


def test_upload_get_and_measure_mixed_kml(client):
    response = upload(client)
    assert response.status_code == 201, response.text
    metadata = response.json()
    UUID(metadata["id"])
    assert response.headers["location"] == f"/api/files/{metadata['id']}/"
    assert metadata["filename"] == "survey.kml"
    assert metadata["feature_count"] == 3
    assert metadata["status"] == "COMPLETED"
    assert metadata["geometry_crs"] == "EPSG:4326"
    assert metadata["crs"]
    assert metadata["created_at"]
    assert metadata["measurement_summary"] == {
        "calculated": 2,
        "not_applicable": 1,
        "unsupported": 0,
        "invalid": 0,
    }

    saved = client.get(response.headers["location"])
    assert saved.status_code == 200
    assert saved.json() == metadata
    result = measurements(client, metadata["id"])
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["file_id"] == metadata["id"]
    assert (payload["total"], payload["limit"], payload["offset"]) == (3, 100, 0)
    assert [item["index"] for item in payload["items"]] == [0, 1, 2]
    assert len({item["feature_id"] for item in payload["items"]}) == 3
    assert all(item["crs"] == "EPSG:4326" for item in payload["items"])

    polygon, line, point = payload["items"]
    assert polygon["geometry_type"] == "Polygon"
    assert polygon["geometry"]["type"] == "Polygon"
    assert polygon["properties"]["owner"] == "Ada"
    assert polygon["measurement"]["status"] == "CALCULATED"
    assert polygon["measurement"]["area_m2"] == pytest.approx(12_309, rel=0.01)
    assert polygon["measurement"]["crs"]
    assert line["geometry_type"] == "LineString"
    assert line["measurement"]["status"] == "CALCULATED"
    assert line["measurement"]["length_m"] == pytest.approx(111.32, rel=0.01)
    assert line["measurement"]["area_m2"] is None
    assert point["geometry_type"] == "Point"
    assert point["measurement"]["status"] == "NOT_APPLICABLE"
    assert point["measurement"]["area_m2"] is None
    assert point["measurement"]["length_m"] is None


def test_holes_are_subtracted_and_multigeometry_is_preserved(client):
    content = kml(
        """
        <Placemark><name>Courtyard</name><Polygon>
          <outerBoundaryIs><LinearRing><coordinates>
            0,0 0.003,0 0.003,0.003 0,0.003 0,0
          </coordinates></LinearRing></outerBoundaryIs>
          <innerBoundaryIs><LinearRing><coordinates>
            0.001,0.001 0.001,0.002 0.002,0.002 0.002,0.001 0.001,0.001
          </coordinates></LinearRing></innerBoundaryIs>
        </Polygon></Placemark>
        <Placemark><name>Two roads</name><MultiGeometry>
          <LineString><coordinates>0,0 0.001,0</coordinates></LineString>
          <LineString><coordinates>0,0.002 0.001,0.002</coordinates></LineString>
        </MultiGeometry></Placemark>
        """
    )
    response = upload(client, content)
    assert response.status_code == 201, response.text
    assert response.json()["feature_count"] == 2
    items = measurements(client, response.json()["id"]).json()["items"]
    courtyard, roads = items
    assert len(courtyard["geometry"]["coordinates"]) == 2
    assert courtyard["measurement"]["area_m2"] == pytest.approx(8 * 12_309, rel=0.01)
    assert roads["geometry_type"] in {"MultiLineString", "GeometryCollection"}
    assert roads["measurement"]["status"] == "CALCULATED"
    assert roads["measurement"]["length_m"] == pytest.approx(2 * 111.32, rel=0.01)


def test_partly_unsupported_multigeometry_does_not_return_partial_measurements(client):
    content = kml(
        """
        <Placemark><name>Road and model</name><MultiGeometry>
          <LineString><coordinates>0,0 0.001,0</coordinates></LineString>
          <Model><Location><longitude>0</longitude><latitude>0</latitude></Location></Model>
        </MultiGeometry></Placemark>
        """
    )
    response = upload(client, content)
    assert response.status_code == 201, response.text
    metadata = response.json()
    assert metadata["measurement_summary"] == {
        "calculated": 0,
        "not_applicable": 0,
        "unsupported": 1,
        "invalid": 0,
    }
    feature = measurements(client, metadata["id"]).json()["items"][0]
    assert feature["measurement"]["status"] == "UNSUPPORTED"
    assert feature["measurement"]["area_m2"] is None
    assert feature["measurement"]["length_m"] is None
    assert feature["measurement"]["reason"]


def test_user_properties_cannot_override_geometry_support(client):
    content = kml(
        """
        <Placemark><ExtendedData>
          <Data name="_unsupported_geometry"><value>Model</value></Data>
        </ExtendedData><LineString><coordinates>0,0 0.001,0</coordinates></LineString></Placemark>
        """
    )
    response = upload(client, content)
    assert response.status_code == 201, response.text
    feature = measurements(client, response.json()["id"]).json()["items"][0]
    assert feature["properties"]["_unsupported_geometry"] == "Model"
    assert feature["geometry_type"] == "LineString"
    assert feature["measurement"]["status"] == "CALCULATED"
    assert feature["measurement"]["length_m"] == pytest.approx(111.32, rel=0.01)


def test_invalid_feature_is_reported_without_failing_other_features(client):
    content = kml(
        """
        <Placemark><name>Self-intersecting polygon</name><Polygon>
          <outerBoundaryIs><LinearRing><coordinates>
            0,0 0.001,0.001 0,0.001 0.001,0 0,0
          </coordinates></LinearRing></outerBoundaryIs>
        </Polygon></Placemark>
        <Placemark><name>Valid point</name><Point><coordinates>0,0</coordinates></Point></Placemark>
        """
    )
    response = upload(client, content)
    assert response.status_code == 201, response.text
    metadata = response.json()
    assert metadata["status"] == "COMPLETED"
    assert metadata["feature_count"] == 2
    assert metadata["measurement_summary"] == {
        "calculated": 0,
        "not_applicable": 1,
        "unsupported": 0,
        "invalid": 1,
    }
    polygon, point = measurements(client, metadata["id"]).json()["items"]
    assert polygon["measurement"]["status"] == "INVALID"
    assert polygon["measurement"]["area_m2"] is None
    assert polygon["measurement"]["length_m"] is None
    assert polygon["measurement"]["reason"]
    assert point["geometry_type"] == "Point"
    assert point["measurement"]["status"] == "NOT_APPLICABLE"
    assert point["geometry"] is not None


def test_projected_shapefile_uses_declared_crs_and_normalizes_geometry(client):
    response = upload(client, shapefile_zip(), "survey.zip")
    assert response.status_code == 201, response.text
    metadata = response.json()
    assert metadata["feature_count"] == 1
    assert "3857" in metadata["crs"]
    feature = measurements(client, metadata["id"]).json()["items"][0]
    assert feature["properties"]["owner"] == "Ada"
    assert feature["geometry_type"] == "Polygon"
    assert feature["crs"] == "EPSG:4326"
    ring = feature["geometry"]["coordinates"][0]
    assert max(point[0] for point in ring) == pytest.approx(0.001, abs=1e-8)
    assert max(point[1] for point in ring) == pytest.approx(0.001, abs=1e-8)
    assert feature["measurement"]["area_m2"] == pytest.approx(12_309, rel=0.01)


def test_uploaded_records_survive_application_restart(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as first:
        response = upload(first)
        assert response.status_code == 201, response.text
        file_id = response.json()["id"]
        original_metadata = response.json()
        original_measurements = measurements(first, file_id).json()
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/files/{file_id}/").json() == original_metadata
        assert measurements(restarted, file_id).json() == original_measurements


def test_measurement_pagination_is_stable(client):
    uploaded = upload(client)
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    all_items = measurements(client, file_id).json()["items"]
    page = measurements(client, file_id, limit=1, offset=1)
    assert page.status_code == 200
    assert page.json() == {
        "file_id": file_id,
        "total": 3,
        "limit": 1,
        "offset": 1,
        "items": [all_items[1]],
    }
    assert measurements(client, file_id, offset=99).json()["items"] == []
    assert measurements(client, file_id, limit=0).status_code == 422
    assert measurements(client, file_id, offset=-1).status_code == 422


@pytest.mark.parametrize("suffix", ["", "measurements/"])
def test_missing_record_returns_404(client, suffix):
    response = client.get(f"/api/files/{uuid4()}/{suffix}")
    assert response.status_code == 404
    assert response.json().get("detail")


@pytest.mark.parametrize("suffix", ["", "measurements/"])
def test_malformed_identifier_returns_422(client, suffix):
    assert client.get(f"/api/files/not-a-uuid/{suffix}").status_code == 422


@pytest.mark.parametrize(
    ("filename", "content", "status"),
    [
        ("survey.txt", MIXED_KML, 415),
        ("survey.kml", b"<kml><Document>", 422),
        ("survey.kml", b"not xml", 422),
        ("survey.kml", b'<?xml version="1.0" encoding="unknown-encoding"?><kml/>', 422),
        ("survey.kml", b"", 422),
        ("survey.zip", b"not a zip file", 422),
    ],
)
def test_invalid_uploads_return_client_errors(client, filename, content, status):
    response = upload(client, content, filename)
    assert response.status_code == status, response.text
    assert response.json().get("detail")


def test_missing_shapefile_projection_is_rejected(client):
    response = upload(client, shapefile_zip(include_prj=False), "survey.zip")
    assert response.status_code == 422, response.text
    assert response.json().get("detail")


def test_upload_limit_is_enforced_before_processing(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_bytes=64))) as api:
        response = upload(api)
    assert response.status_code == 413, response.text


def test_body_limit_rejects_chunked_upload_without_content_length(tmp_path):
    # This deliberately malformed multipart body exceeds the request cap. A 413
    # proves the limit is applied before multipart parsing, which would fail.
    chunks = (b"x" * 16_384 for _ in range(8))
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_bytes=1024))) as api:
        request = api.build_request(
            "POST",
            "/api/files/",
            headers={"Content-Type": "multipart/form-data; boundary=chunked-boundary"},
            content=chunks,
        )
        assert "content-length" not in request.headers
        assert request.headers["transfer-encoding"] == "chunked"
        response = api.send(request)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "upload_too_large"


def test_missing_multipart_file_returns_422(client):
    assert client.post("/api/files/").status_code == 422


def test_xml_entity_declarations_are_rejected(client):
    malicious = b"""<?xml version="1.0"?>
    <!DOCTYPE kml [<!ENTITY payload "unexpected entity expansion">]>
    <kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
      <name>&payload;</name><Point><coordinates>0,0</coordinates></Point>
    </Placemark></Document></kml>"""
    response = upload(client, malicious)
    assert response.status_code == 422, response.text


def test_archive_traversal_is_rejected(client):
    archive = BytesIO()
    with ZipFile(archive, "w") as zf:
        zf.writestr("../outside.shp", b"untrusted content")
    response = upload(client, archive.getvalue(), "survey.zip")
    assert response.status_code == 422, response.text
