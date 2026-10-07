"""
SQLAlchemy ORM Models
=====================
Persistence layer for uploaded geospatial files and their per-feature
measurements.

Design note — denormalised summary columns
------------------------------------------
``GeospatialFile`` stores aggregate counts and totals that could be derived by
scanning ``feature_measurements``. They are computed once during processing and
written alongside the features because the summary and list endpoints are read
far more often than files are uploaded; recomputing them per request would turn
every list call into a full table scan.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class FileStatus(str, enum.Enum):
    """Lifecycle state of an uploaded file.

    ``PENDING`` → ``PROCESSING`` → ``COMPLETED`` | ``FAILED``.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GeospatialFile(Base):
    """An uploaded geospatial file and the outcome of processing it."""

    __tablename__ = "geospatial_files"

    # ── Identity ─────────────────────────────────────────────────────────
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)  # "shapefile" | "kml"
    file_path = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    content_hash = Column(String(64), nullable=True, index=True)

    # ── CRS handling ─────────────────────────────────────────────────────
    crs = Column(String(100), nullable=True)
    projected_crs = Column(String(100), nullable=True)
    crs_strategy = Column(String(50), nullable=True)
    crs_rationale = Column(Text, nullable=True)
    crs_assumed = Column(Boolean, default=False, nullable=False)

    # ── Aggregate results ────────────────────────────────────────────────
    feature_count = Column(Integer, default=0, nullable=False)
    measured_feature_count = Column(Integer, default=0, nullable=False)
    unsupported_feature_count = Column(Integer, default=0, nullable=False)
    invalid_geometry_count = Column(Integer, default=0, nullable=False)
    total_area_sq_meters = Column(Float, nullable=True)
    total_length_meters = Column(Float, nullable=True)
    geometry_type_counts = Column(Text, nullable=True)  # JSON object

    # ── Status & timing ──────────────────────────────────────────────────
    status = Column(
        SAEnum(FileStatus), default=FileStatus.PENDING, nullable=False, index=True
    )
    error_code = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    processing_ms = Column(Integer, nullable=True)
    uploaded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    processed_at = Column(DateTime(timezone=True), nullable=True)

    measurements = relationship(
        "FeatureMeasurement",
        back_populates="geospatial_file",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def geometry_types(self) -> dict[str, int]:
        """Decode the stored geometry-type histogram."""
        if not self.geometry_type_counts:
            return {}
        try:
            return json.loads(self.geometry_type_counts)
        except json.JSONDecodeError:
            return {}

    def __repr__(self) -> str:
        return (
            f"<GeospatialFile(id={self.id!r}, filename={self.filename!r}, "
            f"status={self.status.value if self.status else None!r})>"
        )


class FeatureMeasurement(Base):
    """One feature extracted from a file, with its computed measurements."""

    __tablename__ = "feature_measurements"
    __table_args__ = (
        # Measurements are always fetched for one file, ordered by index.
        Index("ix_feature_file_index", "file_id", "feature_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(
        String(36),
        ForeignKey("geospatial_files.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Feature identity ─────────────────────────────────────────────────
    feature_index = Column(Integer, nullable=False)
    feature_id = Column(String(255), nullable=True)

    # ── Geometry ─────────────────────────────────────────────────────────
    geometry_type = Column(String(50), nullable=False)
    geometry_wkt = Column(Text, nullable=True)
    geometry_geojson = Column(Text, nullable=True)

    # ── CRS ──────────────────────────────────────────────────────────────
    original_crs = Column(String(100), nullable=True)
    projected_crs = Column(String(100), nullable=True)

    # ── Measurements (metres / square metres) ────────────────────────────
    area_sq_meters = Column(Float, nullable=True)
    perimeter_meters = Column(Float, nullable=True)
    length_meters = Column(Float, nullable=True)

    # ── Geometry quality signals ─────────────────────────────────────────
    vertex_count = Column(Integer, default=0, nullable=False)
    part_count = Column(Integer, default=1, nullable=False)
    is_valid = Column(Boolean, default=True, nullable=False)
    validity_reason = Column(Text, nullable=True)
    is_measurable = Column(Boolean, default=False, nullable=False)

    # ── Attributes ───────────────────────────────────────────────────────
    properties = Column(Text, nullable=True)  # JSON object
    measurement_note = Column(Text, nullable=True)

    geospatial_file = relationship("GeospatialFile", back_populates="measurements")

    @property
    def attributes(self) -> dict[str, Any]:
        """Decode the stored attribute JSON into a dict."""
        if not self.properties:
            return {}
        try:
            return json.loads(self.properties)
        except json.JSONDecodeError:
            return {}

    def __repr__(self) -> str:
        return (
            f"<FeatureMeasurement(index={self.feature_index}, "
            f"type={self.geometry_type!r})>"
        )
