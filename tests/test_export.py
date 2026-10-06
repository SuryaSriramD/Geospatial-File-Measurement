"""Browser downloads contain every stored feature, with safe attachment headers."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repository import Repository


def test_export_contains_all_features_across_batch_boundary(tmp_path, monkeypatch):
    placemarks = "".join(
        f'<Placemark id="point-{index}"><name>Point {index}</name>'
        "<Point><coordinates>78.4867,17.385</coordinates></Point></Placemark>"
        for index in range(1001)
    )
    content = (
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{placemarks}</Document></kml>'
    ).encode()
    calls = []
    original_get_features = Repository.get_features

    def track_batches(self, file_id, limit, offset):
        calls.append((limit, offset))
        return original_get_features(self, file_id, limit, offset)

    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        uploaded = api.post("/api/files/", files={"file": ("many-points.kml", content)})
        assert uploaded.status_code == 201, uploaded.text
        file_id = uploaded.json()["id"]
        expected = api.get(f"/api/files/{file_id}/measurements/?limit=1000").json()["items"]
        expected += api.get(f"/api/files/{file_id}/measurements/?offset=1000").json()["items"]
        monkeypatch.setattr(Repository, "get_features", track_batches)
        response = api.get(f"/api/files/{file_id}/export/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.headers["content-disposition"] == 'attachment; filename="many-points-results.json"'
    )
    assert calls == [(1000, 0), (1000, 1000)]
    assert response.json() == {
        "file": uploaded.json(),
        "measurements": {"file_id": file_id, "total": 1001, "items": expected},
    }


def test_export_preserves_measurements_and_sanitizes_attachment_name(tmp_path):
    content = (Path(__file__).resolve().parent.parent / "examples" / "sample.kml").read_bytes()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        uploaded = api.post("/api/files/", files={"file": ('Δ;"survey.kml', content)})
        assert uploaded.status_code == 201, uploaded.text
        file_id = uploaded.json()["id"]
        response = api.get(f"/api/files/{file_id}/export/")
        measurements = api.get(f"/api/files/{file_id}/measurements/").json()
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.isascii()
    assert disposition.startswith('attachment; filename="')
    assert disposition.count('"') == 2
    assert ";" not in disposition.split("filename=", 1)[1]
    assert response.json()["file"] == uploaded.json()
    assert response.json()["measurements"]["items"] == measurements["items"]
    assert any(item["measurement"]["area_m2"] is not None for item in measurements["items"])


def test_missing_export_returns_json_error_before_streaming(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        response = api.get(f"/api/files/{uuid4()}/export/")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "file_not_found"
    assert "content-disposition" not in response.headers
