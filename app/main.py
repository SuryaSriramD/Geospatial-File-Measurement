"""REST endpoints and a same-origin browser interface for file processing."""

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.jobs import JobCapacityError, JobManager
from app.middleware import UploadBodyLimitMiddleware
from app.parsers import ParseError
from app.processing import (
    UploadValidationError,
    process_upload,
    validate_content,
    validate_filename,
)
from app.repository import Repository
from app.schemas import FileInfo, JobStatus, MeasurementPage

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
EXAMPLE_FILE = Path(__file__).resolve().parent.parent / "examples" / "sample.kml"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(settings.data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository.initialize()
        app.state.jobs = JobManager(settings, repository)
        try:
            yield
        finally:
            await run_in_threadpool(app.state.jobs.shutdown)

    app = FastAPI(
        title="Geospatial File Measurement API",
        version="1.1.0",
        description="Upload KML or a zipped Shapefile and retrieve projected measurements in SI units.",
        lifespan=lifespan,
    )
    # Allow 64 KiB for multipart boundaries/headers in addition to the file.
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=settings.max_upload_bytes + 65536)
    app.mount("/assets", StaticFiles(directory=STATIC_DIR, check_dir=False), name="assets")

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request, exc: sqlite3.Error):
        logger.error("Database operation failed", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "storage_unavailable",
                    "message": "Storage is temporarily unavailable.",
                }
            },
        )

    def find_file(file_id: UUID) -> dict:
        metadata = repository.get_file(str(file_id))
        if metadata is None:
            raise HTTPException(
                404, detail={"code": "file_not_found", "message": "File was not found."}
            )
        return metadata

    def read_upload(file: UploadFile) -> tuple[str, bytes]:
        try:
            filename = validate_filename(file.filename)
            content = file.file.read(settings.max_upload_bytes + 1)
            validate_content(content, settings)
        except UploadValidationError as exc:
            raise HTTPException(
                exc.status_code, detail={"code": exc.code, "message": str(exc)}
            ) from exc
        finally:
            file.file.close()
        return filename, content

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config", tags=["Browser"])
    def config() -> dict[str, int]:
        return {
            "max_upload_bytes": settings.max_upload_bytes,
            "max_features": settings.max_features,
        }

    @app.get("/api/example-file", tags=["Browser"])
    def example_file() -> FileResponse:
        return FileResponse(
            EXAMPLE_FILE,
            media_type="application/vnd.google-earth.kml+xml",
            filename="sample.kml",
        )

    @app.get("/health", tags=["Health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/files/",
        status_code=201,
        response_model=FileInfo,
        tags=["Files"],
        responses={
            413: {"description": "Upload too large"},
            415: {"description": "Unsupported file extension"},
            422: {"description": "Malformed or invalid geospatial file"},
        },
    )
    def upload_file(response: Response, file: Annotated[UploadFile, File()]) -> FileInfo:
        filename, content = read_upload(file)
        try:
            metadata = process_upload(filename, content, settings, repository)
        except ParseError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_geospatial_file", "message": str(exc)}
            ) from exc
        response.headers["Location"] = f"/api/files/{metadata.id}/"
        return metadata

    @app.post(
        "/api/jobs/",
        status_code=202,
        response_model=JobStatus,
        tags=["Processing"],
        responses={
            413: {"description": "Upload too large"},
            415: {"description": "Unsupported file extension"},
            422: {"description": "Missing or invalid upload"},
            429: {"description": "Processing queue is full"},
        },
        description=(
            "Submit a file for background processing. Poll the Location URL for progress. "
            "Jobs are process-local and temporary; completed file results persist in SQLite."
        ),
    )
    def submit_job(response: Response, file: Annotated[UploadFile, File()]) -> JobStatus:
        filename, content = read_upload(file)
        try:
            job = app.state.jobs.submit(filename, content)
        except JobCapacityError as exc:
            raise HTTPException(
                429,
                detail={"code": "processing_busy", "message": str(exc)},
                headers={"Retry-After": "2"},
            ) from exc
        response.headers["Location"] = f"/api/jobs/{job.id}/"
        response.headers["Cache-Control"] = "no-store"
        return job

    @app.get("/api/jobs/{job_id}/", response_model=JobStatus, tags=["Processing"])
    def job_status(job_id: UUID, response: Response) -> JobStatus:
        job = app.state.jobs.get(str(job_id))
        if job is None:
            raise HTTPException(
                404,
                detail={
                    "code": "job_not_found",
                    "message": "Job was not found. Job progress expires or resets with the server.",
                },
            )
        response.headers["Cache-Control"] = "no-store"
        return job

    @app.get("/api/files/{file_id}/", response_model=FileInfo, tags=["Files"])
    def file_info(file_id: UUID) -> dict:
        return find_file(file_id)

    @app.get("/api/files/{file_id}/export/", tags=["Files"])
    def export_file(file_id: UUID) -> StreamingResponse:
        metadata = find_file(file_id)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(metadata["filename"]).stem)
        filename = f"{stem.strip('._-')[:120] or 'geospatial'}-results.json"

        def chunks() -> Iterator[str]:
            yield '{"file":' + json.dumps(metadata, allow_nan=False)
            yield ',"measurements":{"file_id":' + json.dumps(str(file_id))
            yield ',"total":' + str(metadata["feature_count"]) + ',"items":['
            first = True
            for offset in range(0, metadata["feature_count"], 1000):
                for feature in repository.get_features(str(file_id), 1000, offset):
                    if not first:
                        yield ","
                    yield json.dumps(feature, allow_nan=False)
                    first = False
            yield "]}}"

        return StreamingResponse(
            chunks(),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    @app.get(
        "/api/files/{file_id}/measurements/", response_model=MeasurementPage, tags=["Measurements"]
    )
    def measurements(
        file_id: UUID,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        metadata = find_file(file_id)
        return {
            "file_id": file_id,
            "total": metadata["feature_count"],
            "limit": limit,
            "offset": offset,
            "items": repository.get_features(str(file_id), limit, offset),
        }

    return app


app = create_app()
