"""Small, explicit configuration surface for a local service."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GEO_DATA_DIR", "data")))
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.getenv("GEO_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    )
    max_features: int = field(default_factory=lambda: int(os.getenv("GEO_MAX_FEATURES", "10000")))

    def __post_init__(self) -> None:
        if self.max_upload_bytes < 1 or self.max_features < 1:
            raise ValueError("Upload and feature limits must be positive integers.")
