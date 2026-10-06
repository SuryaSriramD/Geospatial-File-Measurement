"""Shared upload processing with progress emitted only when real work advances."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from time import perf_counter
from typing import Literal
from uuid import uuid4

from app.config import Settings
from app.measurements import measure_feature
from app.parsers import parse_file
from app.repository import Repository
from app.schemas import FeatureResult, FileInfo, MeasurementSummary

StepKey = Literal["validate", "parse", "measure", "save"]


class UploadValidationError(ValueError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def validate_filename(raw_filename: str | None) -> str:
    filename = PurePosixPath((raw_filename or "").replace("\\", "/")).name
    if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
        raise UploadValidationError(422, "invalid_filename", "A valid filename is required.")
    if PurePosixPath(filename).suffix.lower() not in {".kml", ".zip"}:
        raise UploadValidationError(
            415,
            "unsupported_file_type",
            "Upload a .kml file or a .zip containing a Shapefile.",
        )
    return filename


def validate_content(content: bytes, settings: Settings) -> None:
    if len(content) > settings.max_upload_bytes:
        raise UploadValidationError(413, "upload_too_large", "File exceeds the upload limit.")
    if not content:
        raise UploadValidationError(422, "invalid_geospatial_file", "The uploaded file is empty.")


@dataclass(frozen=True)
class ProgressUpdate:
    key: StepKey
    status: Literal["RUNNING", "COMPLETED", "FAILED"]
    detail: str | None = None
    duration_ms: float | None = None
    processed: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ProgressUpdate], None]


def process_upload(
    filename: str,
    content: bytes,
    settings: Settings,
    repository: Repository,
    progress: ProgressCallback | None = None,
) -> FileInfo:
    def report(update: ProgressUpdate) -> None:
        if progress:
            progress(update)

    @contextmanager
    def step(key: StepKey, detail: str) -> Iterator[dict]:
        started = perf_counter()
        report(ProgressUpdate(key, "RUNNING", detail))
        completed: dict = {}
        try:
            yield completed
        except Exception:
            report(
                ProgressUpdate(
                    key,
                    "FAILED",
                    "This step could not finish.",
                    (perf_counter() - started) * 1000,
                )
            )
            raise
        report(
            ProgressUpdate(
                key,
                "COMPLETED",
                duration_ms=(perf_counter() - started) * 1000,
                **completed,
            )
        )

    with step("validate", "Checking the file name, extension, and upload size.") as done:
        filename = validate_filename(filename)
        validate_content(content, settings)
        done["detail"] = f"Validated {len(content):,} uploaded bytes."

    with step("parse", "Reading geometries, attributes, and the source coordinate system.") as done:
        dataset = parse_file(filename, content, max_features=settings.max_features)
        done.update(
            detail=f"Read {len(dataset.features):,} features in {dataset.crs.to_string()}.",
            total=len(dataset.features),
        )

    features = []
    summary = MeasurementSummary()
    with step("measure", "Checking and measuring each feature.") as done:
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
            report(
                ProgressUpdate(
                    "measure",
                    "RUNNING",
                    f"Processed {index + 1:,} of {len(dataset.features):,} features.",
                    processed=index + 1,
                )
            )
        done["detail"] = f"Processed all {len(features):,} features."

    metadata = FileInfo(
        id=uuid4(),
        filename=filename,
        feature_count=len(features),
        crs=dataset.crs.to_string(),
        created_at=datetime.now(timezone.utc),
        measurement_summary=summary,
    )
    with step("save", "Saving file metadata and measurements in SQLite.") as done:
        repository.save(metadata, features)
        done["detail"] = "File metadata and all feature results were saved."
    return metadata
