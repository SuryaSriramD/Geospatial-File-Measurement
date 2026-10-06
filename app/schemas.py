"""Public REST response contracts; measurements always have explicit units."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Measurement(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    status: Literal["CALCULATED", "NOT_APPLICABLE", "UNSUPPORTED", "INVALID"]
    area_m2: float | None = None
    length_m: float | None = None
    crs: str | None = None
    reason: str | None = None


class MeasurementSummary(BaseModel):
    calculated: int = 0
    not_applicable: int = 0
    unsupported: int = 0
    invalid: int = 0


class FileInfo(BaseModel):
    id: UUID
    filename: str
    feature_count: int
    crs: str
    geometry_crs: Literal["EPSG:4326"] = "EPSG:4326"
    status: Literal["COMPLETED"] = "COMPLETED"
    created_at: datetime
    measurement_summary: MeasurementSummary


class FeatureResult(BaseModel):
    feature_id: str
    index: int
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: Literal["EPSG:4326"] = "EPSG:4326"
    properties: dict[str, Any]
    measurement: Measurement


class MeasurementPage(BaseModel):
    file_id: UUID
    total: int
    limit: int
    offset: int
    items: list[FeatureResult]
