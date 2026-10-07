"""
Geospatial Processing Pipeline
==============================
Orchestrates the journey from an uploaded file on disk to persisted,
per-feature measurements.

Pipeline stages
---------------
1. **Validate** — for archives, verify ZIP safety and shapefile completeness
   (:mod:`app.archive`) before handing bytes to a driver.
2. **Read** — load the dataset into a GeoDataFrame via GeoPandas/Fiona.
3. **Resolve CRS** — choose a metre-based projected CRS and reproject
   (:mod:`app.crs`). No measurement is ever taken on angular coordinates.
4. **Measure** — compute per-feature area/length with the pure engine in
   :mod:`app.measurement`.
5. **Persist** — bulk-write features and a pre-computed dataset summary.

Failure of any single *feature* is contained to that feature; failure of a
*stage* marks the file FAILED with a typed error code the API can surface.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from sqlalchemy import select

from app.archive import validate_shapefile_archive
from app.crs import CRSDecision, resolve_measurement_crs, to_authority_code
from app.database import async_session_factory
from app.exceptions import (
    EmptyDatasetError,
    GeospatialError,
    UnreadableGeospatialFileError,
)
from app.measurement import DatasetSummary, Measurement, measure
from app.models import FeatureMeasurement, FileStatus, GeospatialFile

logger = logging.getLogger(__name__)

# KML is not enabled in Fiona's default driver set and must be opted into
# explicitly before any KML read is attempted.
import fiona  # noqa: E402  (import order is deliberate: patch after logging setup)

fiona.drvsupport.supported_drivers["KML"] = "rw"
fiona.drvsupport.supported_drivers["LIBKML"] = "rw"

#: Geometry WKT longer than this is truncated in storage. Full precision stays
#: available through the GeoJSON endpoint; this keeps row sizes sane for the
#: dense multipolygons common in cadastral data.
MAX_WKT_LENGTH = 100_000


# ══════════════════════════════════════════════════════════════════════════
#  Property normalisation
# ══════════════════════════════════════════════════════════════════════════

def _json_safe(value: Any) -> Any:
    """Coerce one attribute value into something ``json.dumps`` accepts.

    Shapefile and KML attribute tables routinely carry NumPy scalars, pandas
    ``NaT``/``NaN``, timestamps and bytes. Left alone these either raise or
    serialise into invalid JSON (bare ``NaN`` is not valid JSON), so each is
    mapped onto a JSON-native equivalent here.

    Args:
        value: A raw attribute value from a GeoDataFrame row.

    Returns:
        A JSON-serialisable value; None for any null-like input.
    """
    if value is None or value is pd.NaT:
        return None
    # NumPy scalars expose .item() to unwrap into a Python scalar.
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return str(value)


def extract_properties(row: pd.Series, geometry_column: str) -> dict[str, Any]:
    """Build a JSON-safe attribute dict for one feature.

    Args:
        row: A GeoDataFrame row.
        geometry_column: Name of the active geometry column to exclude.

    Returns:
        Attribute name to JSON-safe value.
    """
    return {
        str(key): _json_safe(value)
        for key, value in row.items()
        if key != geometry_column
    }


# ══════════════════════════════════════════════════════════════════════════
#  Reading
# ══════════════════════════════════════════════════════════════════════════

def read_dataset(file_path: Path, file_type: str) -> gpd.GeoDataFrame:
    """Load a geospatial file into a GeoDataFrame.

    Args:
        file_path: Absolute path to the stored upload.
        file_type: Either ``"shapefile"`` or ``"kml"``.

    Returns:
        The loaded dataset.

    Raises:
        CorruptArchiveError / InvalidShapefileError: Archive validation failed.
        UnreadableGeospatialFileError: The driver could not parse the file.
        EmptyDatasetError: The file parsed but holds no features.
    """
    if not file_path.exists():
        raise UnreadableGeospatialFileError(
            "Uploaded file is no longer available on disk.",
            detail=str(file_path),
        )

    try:
        if file_type == "shapefile":
            info = validate_shapefile_archive(file_path)
            logger.info(
                "Archive validated: layer=%s entries=%d prj=%s",
                info.layer_name,
                info.entry_count,
                info.has_projection,
            )
            # The ``zip://`` virtual filesystem lets GDAL read the layer in
            # place, with no extraction to a temp directory and therefore no
            # extraction-time attack surface.
            gdf = gpd.read_file(f"zip://{file_path}!{info.shp_entry}")
        else:
            gdf = gpd.read_file(file_path)

    except GeospatialError:
        raise
    except Exception as exc:
        raise UnreadableGeospatialFileError(
            "The geospatial file could not be parsed.",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    if gdf is None or len(gdf) == 0:
        raise EmptyDatasetError(
            "The geospatial file parsed successfully but contains no features."
        )

    return gdf


# ══════════════════════════════════════════════════════════════════════════
#  Core processing
# ══════════════════════════════════════════════════════════════════════════

def build_feature_rows(
    gdf: gpd.GeoDataFrame,
    projected: gpd.GeoDataFrame,
    decision: CRSDecision,
    file_id: str,
) -> tuple[list[FeatureMeasurement], DatasetSummary]:
    """Measure every feature and build its ORM row.

    Source and projected frames are paired **by positional index** using
    ``.iloc``, which is safe because :func:`app.crs.resolve_measurement_crs`
    preserves row order — unlike zipping two ``iterrows()`` generators, which
    silently mispairs if either frame is reindexed.

    Args:
        gdf: Dataset in its source CRS, used for geometry output and attributes.
        projected: The same dataset in a metre-based CRS, used for measurement.
        decision: The CRS decision record to stamp onto each row.
        file_id: Owning file's UUID.

    Returns:
        ``(rows, summary)``.
    """
    rows: list[FeatureMeasurement] = []
    summary = DatasetSummary()
    geometry_column = gdf.geometry.name

    for position in range(len(gdf)):
        source_row = gdf.iloc[position]
        source_geom = source_row.geometry
        projected_geom = projected.iloc[position].geometry

        m: Measurement = measure(projected_geom)
        summary.add(m)

        # Preserve the dataset's own identifier when one exists; fall back to
        # positional index, which the spec accepts as a feature ID.
        native_id = gdf.index[position]
        feature_id = str(native_id) if not isinstance(native_id, int) else None

        wkt = None
        geojson = None
        if source_geom is not None and not source_geom.is_empty:
            wkt = source_geom.wkt
            if len(wkt) > MAX_WKT_LENGTH:
                wkt = wkt[:MAX_WKT_LENGTH] + "...[truncated]"
            try:
                from shapely.geometry import mapping

                geojson = json.dumps(mapping(source_geom))
            except Exception:  # pragma: no cover - defensive
                geojson = None

        properties = extract_properties(source_row, geometry_column)

        rows.append(
            FeatureMeasurement(
                file_id=file_id,
                feature_index=position,
                feature_id=feature_id,
                geometry_type=m.geometry_type,
                geometry_wkt=wkt,
                geometry_geojson=geojson,
                original_crs=decision.source_crs,
                projected_crs=decision.projected_crs,
                area_sq_meters=m.area_sq_meters,
                perimeter_meters=m.perimeter_meters,
                length_meters=m.length_meters,
                vertex_count=m.vertex_count,
                part_count=m.part_count,
                is_valid=m.is_valid,
                validity_reason=m.validity_reason,
                is_measurable=m.is_supported
                and (m.area_sq_meters is not None or m.length_meters is not None),
                properties=json.dumps(properties),
                measurement_note=m.note,
            )
        )

    return rows, summary


async def process_file(file_id: str, file_path: str, file_type: str) -> None:
    """Run the full pipeline for one uploaded file and persist the outcome.

    This coroutine owns its own database session because it runs detached from
    the request that triggered it; the request's session is long gone by then.

    Args:
        file_id: UUID of the ``GeospatialFile`` row to process.
        file_path: Absolute path to the stored upload.
        file_type: ``"shapefile"`` or ``"kml"``.
    """
    started = time.perf_counter()
    logger.info("Processing file %s (%s)", file_id, file_type)

    async with async_session_factory() as session:
        record = (
            await session.execute(
                select(GeospatialFile).where(GeospatialFile.id == file_id)
            )
        ).scalars().first()

        if record is None:
            logger.error("File record %s not found; aborting", file_id)
            return

        try:
            record.status = FileStatus.PROCESSING
            await session.commit()

            # ── 1-2. Validate and read ──────────────────────────────────
            gdf = read_dataset(Path(file_path).resolve(), file_type)

            # ── 3. CRS resolution ───────────────────────────────────────
            projected, decision = resolve_measurement_crs(gdf)

            # ── 4. Measure ──────────────────────────────────────────────
            rows, summary = build_feature_rows(gdf, projected, decision, file_id)

            # ── 5. Persist ──────────────────────────────────────────────
            record.crs = decision.source_crs
            record.projected_crs = decision.projected_crs
            record.crs_strategy = decision.strategy
            record.crs_rationale = decision.rationale
            record.crs_assumed = decision.assumed_source
            record.feature_count = summary.total_features
            record.measured_feature_count = summary.measured_features
            record.unsupported_feature_count = summary.unsupported_features
            record.invalid_geometry_count = summary.invalid_geometries
            record.total_area_sq_meters = summary.total_area_sq_meters
            record.total_length_meters = summary.total_length_meters
            record.geometry_type_counts = json.dumps(summary.geometry_type_counts)

            session.add_all(rows)

            record.status = FileStatus.COMPLETED
            record.processed_at = datetime.now(timezone.utc)
            record.processing_ms = int((time.perf_counter() - started) * 1000)
            record.error_message = None
            record.error_code = None

            await session.commit()
            logger.info(
                "Processed %s: %d features in %dms",
                file_id,
                summary.total_features,
                record.processing_ms,
            )

        except GeospatialError as exc:
            await _mark_failed(session, record, exc.code, exc.message, exc.detail, started)
            logger.warning("Processing failed for %s: %s", file_id, exc.message)

        except Exception as exc:  # pragma: no cover - last-resort guard
            await _mark_failed(
                session,
                record,
                "INTERNAL_ERROR",
                "An unexpected error occurred while processing the file.",
                f"{type(exc).__name__}: {exc}",
                started,
            )
            logger.exception("Unexpected failure processing %s", file_id)


async def _mark_failed(
    session,
    record: GeospatialFile,
    code: str,
    message: str,
    detail: str | None,
    started: float,
) -> None:
    """Record a processing failure against the file row.

    Rolls back first so a partially-flushed batch of feature rows cannot leave
    the file marked FAILED while orphan measurements remain committed.
    """
    await session.rollback()
    record = await session.merge(record)
    record.status = FileStatus.FAILED
    record.error_code = code
    record.error_message = f"{message} {detail}".strip() if detail else message
    record.processed_at = datetime.now(timezone.utc)
    record.processing_ms = int((time.perf_counter() - started) * 1000)
    await session.commit()


def process_file_blocking(file_id: str, file_path: str, file_type: str) -> None:
    """Synchronous entry point for running the pipeline in a worker thread.

    GeoPandas and GDAL are CPU-bound and release no event loop time, so the
    API layer dispatches this through ``asyncio.to_thread``. Each thread gets
    its own event loop via ``asyncio.run``.

    Args:
        file_id: UUID of the file record.
        file_path: Absolute path to the stored upload.
        file_type: ``"shapefile"`` or ``"kml"``.
    """
    import asyncio

    asyncio.run(process_file(file_id, file_path, file_type))
