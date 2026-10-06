"""Real progress, bounded capacity, and durable results for browser processing jobs."""

import sqlite3
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import processing
from app.config import Settings
from app.jobs import JobManager
from app.main import create_app
from app.repository import Repository

SAMPLE_KML = (Path(__file__).resolve().parent.parent / "examples" / "sample.kml").read_bytes()


def submit(client, payload=SAMPLE_KML, filename="sample.kml"):
    return client.post("/api/jobs/", files={"file": (filename, payload)})


def terminal(client, job_id):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}/")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"COMPLETED", "FAILED"}:
            return job
        sleep(0.01)
    pytest.fail("Processing job did not finish")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        yield api


def test_job_progress_follows_real_parse_measure_and_save_work(tmp_path, monkeypatch):
    parse_entered, parse_release = Event(), Event()
    second_measure_entered, measure_release = Event(), Event()
    save_entered, save_release = Event(), Event()
    original_parse = processing.parse_file
    original_measure = processing.measure_feature
    original_save = Repository.save
    measurements = 0

    def controlled_parse(*args, **kwargs):
        parse_entered.set()
        assert parse_release.wait(10)
        return original_parse(*args, **kwargs)

    def controlled_measure(*args, **kwargs):
        nonlocal measurements
        measurements += 1
        if measurements == 2:
            second_measure_entered.set()
            assert measure_release.wait(10)
        return original_measure(*args, **kwargs)

    def controlled_save(*args, **kwargs):
        save_entered.set()
        assert save_release.wait(10)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(processing, "parse_file", controlled_parse)
    monkeypatch.setattr(processing, "measure_feature", controlled_measure)
    monkeypatch.setattr(Repository, "save", controlled_save)
    with TestClient(create_app(Settings(data_dir=tmp_path))) as api:
        try:
            response = submit(api)
            assert response.status_code == 202
            initial = response.json()
            UUID(initial["id"])
            assert initial["status"] == "QUEUED"
            assert initial["total"] is None
            assert initial["processed"] == 0
            assert all(step["status"] == "PENDING" for step in initial["steps"])
            assert response.headers["location"] == f"/api/jobs/{initial['id']}/"
            assert parse_entered.wait(5)
            parsing = api.get(response.headers["location"]).json()
            assert parsing["status"] == "PROCESSING"
            assert [step["key"] for step in parsing["steps"]] == [
                "validate",
                "parse",
                "measure",
                "save",
            ]
            assert [step["status"] for step in parsing["steps"]] == [
                "COMPLETED",
                "RUNNING",
                "PENDING",
                "PENDING",
            ]
            assert parsing["steps"][0]["duration_ms"] >= 0
            assert parsing["steps"][1]["duration_ms"] is None
            assert parsing["total"] is None

            parse_release.set()
            assert second_measure_entered.wait(5)
            measuring = api.get(response.headers["location"]).json()
            assert measuring["total"] == 3
            assert measuring["processed"] == 1
            assert [step["status"] for step in measuring["steps"]] == [
                "COMPLETED",
                "COMPLETED",
                "RUNNING",
                "PENDING",
            ]
            measure_release.set()
            assert save_entered.wait(5)
            saving = api.get(response.headers["location"]).json()
            assert saving["processed"] == saving["total"] == 3
            assert saving["status"] == "PROCESSING"
            assert saving["file_id"] is None
            assert saving["steps"][3]["status"] == "RUNNING"
            save_release.set()
            result = terminal(api, initial["id"])
            assert result["status"] == "COMPLETED"
            assert result["error"] is None
            assert all(step["status"] == "COMPLETED" for step in result["steps"])
            assert all(step["duration_ms"] >= 0 for step in result["steps"])
            assert result["created_at"] <= result["updated_at"]
            file_id = result["file_id"]
            assert api.get(f"/api/files/{file_id}/").json()["feature_count"] == 3
            items = api.get(f"/api/files/{file_id}/measurements/").json()["items"]
            assert len(items) == 3
            assert sorted(item["measurement"]["status"] for item in items) == [
                "CALCULATED",
                "CALCULATED",
                "NOT_APPLICABLE",
            ]
        finally:
            parse_release.set()
            measure_release.set()
            save_release.set()


@pytest.mark.parametrize("filename,payload", [("bad.kml", b"not xml"), ("bad.zip", b"not zip")])
def test_malformed_jobs_fail_at_parse_without_creating_file(client, tmp_path, filename, payload):
    response = submit(client, payload, filename)
    assert response.status_code == 202
    result = terminal(client, response.json()["id"])
    assert result["status"] == "FAILED"
    assert result["file_id"] is None
    assert result["error"]["code"] == "invalid_geospatial_file"
    assert result["error"]["message"]
    assert [step["status"] for step in result["steps"]] == [
        "COMPLETED",
        "FAILED",
        "PENDING",
        "PENDING",
    ]
    assert result["steps"][1]["detail"] == result["error"]["message"]
    with Repository(tmp_path).connection() as conn:
        assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload,filename,status",
    [(b"data", "unsupported.txt", 415), (b"", "empty.kml", 422), (SAMPLE_KML, "x.kml", 413)],
)
def test_invalid_uploads_rejected_before_job_submission(tmp_path, payload, filename, status):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_bytes=64))) as api:
        response = submit(api, payload, filename)
        assert response.status_code == status
        assert api.app.state.jobs._jobs == {}


def test_missing_or_unknown_job(client):
    assert client.post("/api/jobs/").status_code == 422
    assert client.get("/api/jobs/not-a-uuid/").status_code == 422
    response = client.get(f"/api/jobs/{uuid4()}/")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "job_not_found"


def test_full_job_queue_returns_retryable_error_and_recovers(client, monkeypatch):
    release = Event()
    original_parse = processing.parse_file

    def waiting_parse(*args, **kwargs):
        assert release.wait(10)
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(processing, "parse_file", waiting_parse)
    accepted = []
    try:
        for _ in range(8):
            response = submit(client)
            assert response.status_code == 202
            accepted.append(response.json()["id"])
        response = submit(client)
        assert response.status_code == 429
        assert response.headers["retry-after"] == "2"
        assert response.json()["detail"]["code"] == "processing_busy"
        assert len(client.app.state.jobs._jobs) == 8
    finally:
        release.set()
    assert all(terminal(client, job_id)["status"] == "COMPLETED" for job_id in accepted)
    assert submit(client).status_code == 202


def test_unexpected_failure_is_reported_without_exposing_internal_details(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("private/server/path and database credentials")

    monkeypatch.setattr(processing, "measure_feature", fail)
    response = submit(client)
    result = terminal(client, response.json()["id"])
    assert result["status"] == "FAILED"
    assert result["error"] == {
        "code": "processing_failed",
        "message": "The file could not be processed.",
    }
    assert result["steps"][2]["status"] == "FAILED"
    assert "private" not in str(result)
    assert result["file_id"] is None


def test_storage_failure_marks_save_step_failed(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("private internal error")

    monkeypatch.setattr(Repository, "save", fail)
    response = submit(client)
    result = terminal(client, response.json()["id"])
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "storage_unavailable"
    assert result["steps"][3]["status"] == "FAILED"
    assert "private" not in str(result)
    assert result["file_id"] is None


def test_status_snapshots_are_independent_and_retention_is_bounded(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = Repository(tmp_path)
    repository.initialize()
    manager = JobManager(settings, repository, max_completed=2)
    ids = []
    try:
        for _ in range(3):
            job = manager.submit("sample.kml", SAMPLE_KML)
            ids.append(str(job.id))
            job.steps[0].label = "changed outside manager"
            deadline = monotonic() + 5
            while manager.get(str(job.id)).status not in {"COMPLETED", "FAILED"}:
                assert monotonic() < deadline
                sleep(0.01)
            assert manager.get(str(job.id)).steps[0].label == "Validate file"
        manager.shutdown()
        assert manager.get(ids[0]) is None
        assert all(manager.get(job_id).status == "COMPLETED" for job_id in ids[1:])
        assert len(manager._jobs) == 2
    finally:
        manager.shutdown()


def test_completed_results_survive_restart_but_progress_is_temporary(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as api:
        response = submit(api)
        job_id = response.json()["id"]
        result = terminal(api, job_id)
        file_id = result["file_id"]
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/jobs/{job_id}/").status_code == 404
        assert restarted.get(f"/api/files/{file_id}/").status_code == 200


def test_browser_config_and_sample_use_same_server(client):
    assert client.get("/api/config").json() == {
        "max_upload_bytes": 10 * 1024 * 1024,
        "max_features": 10000,
    }
    example = client.get("/api/example-file")
    assert example.status_code == 200
    assert example.content == SAMPLE_KML
    assert example.headers["content-type"].startswith("application/vnd.google-earth.kml+xml")
    assert client.get("/health").json() == {"status": "ok"}


def test_retention_keeps_latest_completion_even_when_it_was_submitted_first(tmp_path, monkeypatch):
    first_entered, release_first = Event(), Event()
    original_parse = processing.parse_file

    def out_of_order_parse(filename, *args, **kwargs):
        if filename == "first.kml":
            first_entered.set()
            assert release_first.wait(10)
        return original_parse(filename, *args, **kwargs)

    monkeypatch.setattr(processing, "parse_file", out_of_order_parse)
    settings = Settings(data_dir=tmp_path)
    repository = Repository(tmp_path)
    repository.initialize()
    manager = JobManager(settings, repository, max_completed=1)
    try:
        first_id = str(manager.submit("first.kml", SAMPLE_KML).id)
        assert first_entered.wait(5)
        second_id = str(manager.submit("second.kml", SAMPLE_KML).id)
        deadline = monotonic() + 5
        while manager.get(second_id).status != "COMPLETED":
            assert monotonic() < deadline
            sleep(0.01)
        release_first.set()
        manager.shutdown()
        assert manager.get(second_id) is None
        first = manager.get(first_id)
        assert first is not None
        assert first.status == "COMPLETED"
        assert repository.get_file(str(first.file_id)) is not None
    finally:
        release_first.set()
        manager.shutdown()
