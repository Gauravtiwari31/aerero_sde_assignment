"""
Pydantic Schemas
================
The API's public contract. Every response model carries field descriptions and
an example, which FastAPI renders directly into the OpenAPI schema at ``/docs``.

Measurements are returned in several units. Clients routinely want hectares or
acres for land parcels and kilometres for routes; deriving them server-side
once avoids each consumer re-implementing the conversion and drifting on
rounding.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════
#  Upload & file information
# ══════════════════════════════════════════════════════════════════════════

class FileUploadResponse(BaseModel):
    """Acknowledgement returned the moment an upload is accepted."""

    id: str = Field(..., description="Unique identifier for the uploaded file")
    filename: str = Field(..., description="Original filename as supplied")
    file_type: str = Field(..., description="Detected type: shapefile or kml")
    file_size_bytes: int = Field(..., description="Size of the stored upload")
    status: str = Field(..., description="Processing status at acceptance time")
    message: str = Field(..., description="Human-readable next step")
    links: dict[str, str] = Field(
        ..., description="Follow-up URLs for status and measurements"
    )

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {
            "example": {
                "id": "3f2a9c10-5b7e-4c2d-9a11-7e8f6d4b1c33",
                "filename": "survey.kml",
                "file_type": "kml",
                "file_size_bytes": 24576,
                "status": "PENDING",
                "message": "File accepted and queued for processing.",
                "links": {
                    "self": "/api/files/3f2a9c10-5b7e-4c2d-9a11-7e8f6d4b1c33/",
                    "measurements": "/api/files/3f2a9c10-5b7e-4c2d-9a11-7e8f6d4b1c33/measurements/",
                },
            }
        },
    }


class CRSInfo(BaseModel):
    """How the measurement CRS was chosen for a dataset.

    Surfacing the strategy and rationale makes every measurement auditable: a
    reviewer can see not just the number but the projection it came from and
    why that projection was selected.
    """

    source: Optional[str] = Field(None, description="CRS of the file as supplied")
    projected: Optional[str] = Field(
        None, description="Metre-based CRS used for all measurements"
    )
    strategy: Optional[str] = Field(
        None,
        description="Rule that fired: source_projected, utm_centroid or configured_fallback",
    )
    rationale: Optional[str] = Field(
        None, description="Plain-language explanation of the choice"
    )
    assumed: bool = Field(
        False, description="True when the file declared no CRS and WGS84 was assumed"
    )


class FileInfoResponse(BaseModel):
    """Full status and result summary for one uploaded file."""

    id: str = Field(..., description="Unique identifier")
    filename: str = Field(..., description="Original filename")
    file_type: str = Field(..., description="shapefile or kml")
    file_size_bytes: Optional[int] = Field(None, description="Stored size in bytes")
    feature_count: int = Field(..., description="Total features extracted")
    crs: Optional[str] = Field(None, description="Source CRS, e.g. EPSG:4326")
    crs_details: Optional[CRSInfo] = Field(
        None, description="Full CRS resolution record"
    )
    status: Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"] = Field(
        ..., description="Processing status"
    )
    error_code: Optional[str] = Field(
        None, description="Machine-readable error code when status is FAILED"
    )
    error_message: Optional[str] = Field(None, description="Failure details")
    processing_ms: Optional[int] = Field(
        None, description="Wall-clock processing time in milliseconds"
    )
    uploaded_at: datetime = Field(..., description="Upload timestamp (UTC)")
    processed_at: Optional[datetime] = Field(
        None, description="Completion timestamp (UTC)"
    )

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {
            "example": {
                "id": "abc123",
                "filename": "survey.kml",
                "feature_count": 120,
                "crs": "EPSG:4326",
                "status": "COMPLETED",
            }
        },
    }


class FileListResponse(BaseModel):
    """A page of uploaded files."""

    total: int = Field(..., description="Total matching files across all pages")
    skip: int = Field(..., description="Offset applied to this page")
    limit: int = Field(..., description="Maximum page size requested")
    count: int = Field(..., description="Number of records in this page")
    files: list[FileInfoResponse] = Field(..., description="File records")


# ══════════════════════════════════════════════════════════════════════════
#  Measurements
# ══════════════════════════════════════════════════════════════════════════

class MeasurementItem(BaseModel):
    """Geometry, attributes and measurements for a single feature."""

    feature_index: int = Field(..., description="Zero-based index within the file")
    feature_id: Optional[str] = Field(
        None, description="Native feature identifier when the dataset supplies one"
    )
    geometry_type: str = Field(..., description="OGC geometry type name")
    geometry_wkt: Optional[str] = Field(
        None, description="Geometry as WKT in the source CRS"
    )
    geometry_geojson: Optional[dict] = Field(
        None, description="Geometry as GeoJSON in the source CRS"
    )

    original_crs: Optional[str] = Field(None, description="Source CRS")
    projected_crs: Optional[str] = Field(
        None, description="CRS the measurement was computed in"
    )

    is_measurable: bool = Field(
        ..., description="Whether a numeric measurement was produced"
    )
    area_sq_meters: Optional[float] = Field(None, description="Area in m²")
    area_sq_kilometers: Optional[float] = Field(None, description="Area in km²")
    area_hectares: Optional[float] = Field(None, description="Area in hectares")
    area_acres: Optional[float] = Field(None, description="Area in acres")
    perimeter_meters: Optional[float] = Field(
        None, description="Polygon boundary length in metres"
    )
    length_meters: Optional[float] = Field(None, description="Length in metres")
    length_kilometers: Optional[float] = Field(None, description="Length in km")
    length_miles: Optional[float] = Field(None, description="Length in miles")

    vertex_count: int = Field(0, description="Number of coordinate pairs")
    part_count: int = Field(1, description="Constituent parts for multi-geometries")
    is_valid: bool = Field(True, description="Shapely geometry validity")
    validity_reason: Optional[str] = Field(
        None, description="Why the geometry is invalid, when applicable"
    )

    properties: dict[str, Any] = Field(
        default_factory=dict, description="Feature attributes"
    )
    measurement_note: Optional[str] = Field(
        None, description="Remark, e.g. why no measurement exists"
    )

    model_config = {"from_attributes": True}


class MeasurementSummary(BaseModel):
    """Aggregate totals across every feature in a file."""

    total_features: int = Field(..., description="Features extracted")
    measured_features: int = Field(..., description="Features with a measurement")
    unsupported_features: int = Field(
        ..., description="Features whose geometry type has no measurement"
    )
    invalid_geometries: int = Field(
        ..., description="Features failing a geometry validity check"
    )
    geometry_type_counts: dict[str, int] = Field(
        default_factory=dict, description="Histogram of geometry types"
    )
    total_area_sq_meters: Optional[float] = Field(None, description="Summed area in m²")
    total_area_hectares: Optional[float] = Field(
        None, description="Summed area in hectares"
    )
    total_length_meters: Optional[float] = Field(
        None, description="Summed length in metres"
    )
    total_length_kilometers: Optional[float] = Field(
        None, description="Summed length in km"
    )


class MeasurementsResponse(BaseModel):
    """Paged per-feature measurements plus the dataset summary."""

    file_id: str = Field(..., description="File identifier")
    filename: str = Field(..., description="Original filename")
    crs: Optional[str] = Field(None, description="Source CRS")
    crs_details: Optional[CRSInfo] = Field(None, description="CRS resolution record")
    feature_count: int = Field(..., description="Total features in the file")
    summary: MeasurementSummary = Field(..., description="Aggregate statistics")
    skip: int = Field(0, description="Offset applied")
    limit: int = Field(0, description="Page size requested")
    count: int = Field(0, description="Records in this page")
    measurements: list[MeasurementItem] = Field(..., description="Per-feature results")

    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════════
#  Operational
# ══════════════════════════════════════════════════════════════════════════

class HealthResponse(BaseModel):
    """Liveness and dependency check."""

    status: Literal["ok", "degraded"] = Field(..., description="Overall health")
    version: str = Field(..., description="Application version")
    database: str = Field(..., description="Database connectivity")
    upload_dir_writable: bool = Field(
        ..., description="Whether the upload directory accepts writes"
    )
    supported_formats: list[str] = Field(..., description="Accepted file extensions")


class StatsResponse(BaseModel):
    """Service-wide processing statistics."""

    total_files: int = Field(..., description="Files uploaded")
    by_status: dict[str, int] = Field(..., description="File counts per status")
    total_features: int = Field(..., description="Features processed overall")
    total_area_sq_kilometers: float = Field(..., description="Summed area in km²")
    total_length_kilometers: float = Field(..., description="Summed length in km")
    average_processing_ms: Optional[float] = Field(
        None, description="Mean processing time for completed files"
    )


class ErrorResponse(BaseModel):
    """Standard error envelope used by every non-2xx response."""

    error: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable summary")
    detail: Optional[str] = Field(None, description="Additional context")
    status_code: int = Field(..., description="HTTP status code")

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": "INVALID_SHAPEFILE",
                "message": "Shapefile set is incomplete.",
                "detail": "Layer 'parcels' is missing required sidecar file(s): .dbf.",
                "status_code": 422,
            }
        }
    }
