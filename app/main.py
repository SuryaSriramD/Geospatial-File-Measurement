"""REST endpoints. CPU-bound parsing/projection runs in FastAPI's worker pool."""

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse

from app.config import Settings
from app.measurements import measure_feature
from app.middleware import UploadBodyLimitMiddleware
from app.parsers import ParseError, parse_file
from app.repository import Repository
from app.schemas import FeatureResult, FileInfo, MeasurementPage, MeasurementSummary

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(settings.data_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repository.initialize()
        yield

    app = FastAPI(
        title="Geospatial File Measurement API",
        version="1.0.0",
        description="Upload KML or a zipped Shapefile and retrieve projected measurements in SI units.",
        lifespan=lifespan,
    )
    # Allow 64 KiB for multipart boundaries/headers in addition to the file.
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=settings.max_upload_bytes + 65536)

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
        filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
        if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
            raise HTTPException(
                422, detail={"code": "invalid_filename", "message": "A valid filename is required."}
            )
        if PurePosixPath(filename).suffix.lower() not in {".kml", ".zip"}:
            raise HTTPException(
                415,
                detail={
                    "code": "unsupported_file_type",
                    "message": "Upload a .kml file or a .zip containing a Shapefile.",
                },
            )
        try:
            content = file.file.read(settings.max_upload_bytes + 1)
        finally:
            file.file.close()
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(
                413,
                detail={"code": "upload_too_large", "message": "File exceeds the upload limit."},
            )
        try:
            dataset = parse_file(filename, content, max_features=settings.max_features)
        except ParseError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_geospatial_file", "message": str(exc)}
            ) from exc

        features = []
        summary = MeasurementSummary()
        for index, source in enumerate(dataset.features):
            result = measure_feature(source.geometry, dataset.crs)
            unsupported = source.unsupported_geometry
            if unsupported:
                reason = f"Unsupported input geometry: {unsupported}."
                if source.geometry is not None:
                    reason += " Returned geometry includes only supported components; no partial measurement was calculated."
                result["measurement"].update(
                    status="UNSUPPORTED",
                    reason=reason,
                    area_m2=None,
                    length_m=None,
                    crs=None,
                )
                if source.geometry is None:
                    result["geometry_type"] = str(unsupported)
            feature = FeatureResult(
                feature_id=source.feature_id,
                index=index,
                properties=source.properties,
                **result,
            )
            summary_key = feature.measurement.status.lower()
            setattr(summary, summary_key, getattr(summary, summary_key) + 1)
            features.append(feature)

        metadata = FileInfo(
            id=uuid4(),
            filename=filename,
            feature_count=len(features),
            crs=dataset.crs.to_string(),
            created_at=datetime.now(timezone.utc),
            measurement_summary=summary,
        )
        repository.save(metadata, features)
        response.headers["Location"] = f"/api/files/{metadata.id}/"
        return metadata

    @app.get("/api/files/{file_id}/", response_model=FileInfo, tags=["Files"])
    def file_info(file_id: UUID) -> dict:
        return find_file(file_id)

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
