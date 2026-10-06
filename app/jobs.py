"""Bounded, process-local jobs; completed file results remain in SQLite."""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from app.config import Settings
from app.parsers import ParseError
from app.processing import ProgressUpdate, UploadValidationError, process_upload
from app.repository import Repository
from app.schemas import JobError, JobStatus, ProcessingStep

logger = logging.getLogger(__name__)


class JobCapacityError(RuntimeError):
    """The bounded job queue has no available slot, or is shutting down."""


class JobManager:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        *,
        max_workers: int = 2,
        max_active: int = 8,
        max_completed: int = 100,
    ) -> None:
        if min(max_workers, max_active, max_completed) < 1:
            raise ValueError("Job limits must be positive integers.")
        self.settings = settings
        self.repository = repository
        self.max_active = max_active
        self.max_completed = max_completed
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="geospatial"
        )
        self._lock = Lock()
        self._jobs: dict[str, JobStatus] = {}
        self._active = 0
        self._accepting = True

    def submit(self, filename: str, content: bytes) -> JobStatus:
        with self._lock:
            if not self._accepting or self._active >= self.max_active:
                raise JobCapacityError("Processing is busy. Please try again shortly.")
            now = datetime.now(timezone.utc)
            job = JobStatus(
                id=uuid4(),
                filename=filename,
                created_at=now,
                updated_at=now,
                steps=[
                    ProcessingStep(key="validate", label="Validate file"),
                    ProcessingStep(key="parse", label="Parse features"),
                    ProcessingStep(key="measure", label="Measure geometry"),
                    ProcessingStep(key="save", label="Save results"),
                ],
            )
            job_id = str(job.id)
            self._jobs[job_id] = job
            self._active += 1
            try:
                self._executor.submit(self._run, job_id, filename, content)
            except Exception:
                self._active -= 1
                self._jobs.pop(job_id)
                raise
            return job.model_copy(deep=True)

    def get(self, job_id: str) -> JobStatus | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    def shutdown(self) -> None:
        with self._lock:
            self._accepting = False
        self._executor.shutdown(wait=True)

    def _progress(self, job_id: str, update: ProgressUpdate) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.updated_at = datetime.now(timezone.utc)
            step = next(step for step in job.steps if step.key == update.key)
            step.status = update.status
            step.detail = update.detail
            step.duration_ms = update.duration_ms
            if update.processed is not None:
                job.processed = update.processed
            if update.total is not None:
                job.total = update.total

    def _run(self, job_id: str, filename: str, content: bytes) -> None:
        with self._lock:
            self._jobs[job_id].status = "PROCESSING"
            self._jobs[job_id].updated_at = datetime.now(timezone.utc)
        metadata = None
        error = None
        try:
            metadata = process_upload(
                filename,
                content,
                self.settings,
                self.repository,
                progress=lambda update: self._progress(job_id, update),
            )
        except Exception as exc:
            if isinstance(exc, (ParseError, UploadValidationError)):
                code = (
                    exc.code
                    if isinstance(exc, UploadValidationError)
                    else "invalid_geospatial_file"
                )
                error = JobError(code=code, message=str(exc))
            elif isinstance(exc, sqlite3.Error):
                logger.exception("Job storage failed")
                error = JobError(
                    code="storage_unavailable", message="Storage is temporarily unavailable."
                )
            else:
                logger.exception("Job processing failed")
                error = JobError(
                    code="processing_failed", message="The file could not be processed."
                )
        finally:
            # Store only status metadata. The executor releases the uploaded bytes
            # when this call returns; futures and payloads are never retained.
            with self._lock:
                job = self._jobs[job_id]
                if metadata is not None:
                    job.status = "COMPLETED"
                    job.file_id = metadata.id
                else:
                    job.status = "FAILED"
                    job.error = error or JobError(
                        code="processing_failed", message="The file could not be processed."
                    )
                    for step in job.steps:
                        if step.status == "FAILED":
                            step.detail = job.error.message
                job.updated_at = datetime.now(timezone.utc)
                self._active -= 1
                terminal = sorted(
                    (
                        key
                        for key, value in self._jobs.items()
                        if value.status in {"COMPLETED", "FAILED"}
                    ),
                    key=lambda key: self._jobs[key].updated_at,
                )
                for key in terminal[: max(0, len(terminal) - self.max_completed)]:
                    del self._jobs[key]
