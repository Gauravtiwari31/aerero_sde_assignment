"""
API Endpoints
=============
REST surface for uploading geospatial files and reading their measurements.

Endpoints
---------
``POST   /api/files/``                      Upload and queue a file
``GET    /api/files/``                      List files (paginated, filterable)
``GET    /api/files/{id}/``                 File status and summary
``GET    /api/files/{id}/measurements/``    Per-feature measurements (paginated)
``GET    /api/files/{id}/geojson/``         Measurements as a GeoJSON FeatureCollection
``DELETE /api/files/{id}/``                 Delete a file and its measurements
``GET    /api/health/``                     Liveness and dependency check
``GET    /api/stats/``                      Service-wide statistics
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.exceptions import GeospatialError
from app.geospatial import process_file_blocking
from app.measurement import (
    METRES_PER_KM,
    SQ_METRES_PER_HECTARE,
    SQ_METRES_PER_SQ_KM,
    METRES_PER_MILE,
)
from app.models import FeatureMeasurement, FileStatus, GeospatialFile
from app.schemas import (
    CRSInfo,
    FileInfoResponse,
    FileListResponse,
    FileUploadResponse,
    HealthResponse,
    MeasurementItem,
    MeasurementSummary,
    MeasurementsResponse,
    StatsResponse,
)

router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)

#: Bytes read per iteration while streaming an upload to disk. 1 MiB balances
#: syscall overhead against peak memory for concurrent uploads.
UPLOAD_CHUNK_SIZE = 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════

async def _stream_upload_to_disk(upload: UploadFile, destination: Path) -> tuple[int, str]:
    """Write an upload to disk in chunks, enforcing the size cap as it goes.

    The cap is checked *during* the write rather than after it. Checking
    afterwards means an oversized upload is fully materialised on disk before
    being rejected, which is exactly the resource exhaustion the limit exists
    to prevent.

    Args:
        upload: The incoming multipart file.
        destination: Where to write it.

    Returns:
        ``(bytes_written, sha256_hex)``. The digest supports duplicate
        detection and gives clients an integrity check.

    Raises:
        HTTPException: 413 when the stream exceeds the configured maximum.
    """
    limit = settings.max_upload_size_bytes
    digest = hashlib.sha256()
    written = 0

    try:
        with destination.open("wb") as sink:
            while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
                written += len(chunk)
                if written > limit:
                    sink.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"File exceeds the maximum allowed size of "
                            f"{settings.max_upload_size_mb} MB."
                        ),
                    )
                digest.update(chunk)
                sink.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        logger.error("Failed writing upload to %s: %s", destination, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store the uploaded file.",
        ) from exc
    finally:
        await upload.close()

    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    return written, digest.hexdigest()


async def _get_file_or_404(file_id: str, db: AsyncSession) -> GeospatialFile:
    """Fetch a file record or raise a 404.

    Args:
        file_id: UUID of the record.
        db: Active session.

    Returns:
        The matching ``GeospatialFile``.

    Raises:
        HTTPException: 404 when no record matches.
    """
    record = (
        await db.execute(select(GeospatialFile).where(GeospatialFile.id == file_id))
    ).scalars().first()

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No file found with id {file_id!r}.",
        )
    return record


def _crs_info(record: GeospatialFile) -> CRSInfo:
    """Build the CRS detail block from a file record."""
    return CRSInfo(
        source=record.crs,
        projected=record.projected_crs,
        strategy=record.crs_strategy,
        rationale=record.crs_rationale,
        assumed=bool(record.crs_assumed),
    )


def _file_info(record: GeospatialFile) -> FileInfoResponse:
    """Map a file record onto its response schema."""
    return FileInfoResponse(
        id=record.id,
        filename=record.filename,
        file_type=record.file_type,
        file_size_bytes=record.file_size_bytes,
        feature_count=record.feature_count or 0,
        crs=record.crs,
        crs_details=_crs_info(record),
        status=record.status.value if record.status else "PENDING",
        error_code=record.error_code,
        error_message=record.error_message,
        processing_ms=record.processing_ms,
        uploaded_at=record.uploaded_at,
        processed_at=record.processed_at,
    )


def _ensure_readable(record: GeospatialFile) -> None:
    """Reject reads of measurements that do not exist yet.

    Raises:
        HTTPException: 409 while processing, 422 when processing failed.
    """
    if record.status in (FileStatus.PENDING, FileStatus.PROCESSING):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"File is still being processed (status: {record.status.value}). "
                "Poll GET /api/files/{id}/ until the status is COMPLETED."
            ),
        )
    if record.status == FileStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Processing failed ({record.error_code or 'UNKNOWN'}): "
                f"{record.error_message}"
            ),
        )


def _summary(record: GeospatialFile) -> MeasurementSummary:
    """Build the aggregate summary block from a file's stored totals."""
    area = record.total_area_sq_meters
    length = record.total_length_meters
    return MeasurementSummary(
        total_features=record.feature_count or 0,
        measured_features=record.measured_feature_count or 0,
        unsupported_features=record.unsupported_feature_count or 0,
        invalid_geometries=record.invalid_geometry_count or 0,
        geometry_type_counts=record.geometry_types,
        total_area_sq_meters=area,
        total_area_hectares=(area / SQ_METRES_PER_HECTARE) if area else None,
        total_length_meters=length,
        total_length_kilometers=(length / METRES_PER_KM) if length else None,
    )


def _measurement_item(row: FeatureMeasurement) -> MeasurementItem:
    """Map a stored feature row onto its response schema, deriving units."""
    geojson = None
    if row.geometry_geojson:
        try:
            geojson = json.loads(row.geometry_geojson)
        except json.JSONDecodeError:
            geojson = None

    area = row.area_sq_meters
    length = row.length_meters

    return MeasurementItem(
        feature_index=row.feature_index,
        feature_id=row.feature_id,
        geometry_type=row.geometry_type,
        geometry_wkt=row.geometry_wkt,
        geometry_geojson=geojson,
        original_crs=row.original_crs,
        projected_crs=row.projected_crs,
        is_measurable=bool(row.is_measurable),
        area_sq_meters=area,
        area_sq_kilometers=(area / SQ_METRES_PER_SQ_KM) if area is not None else None,
        area_hectares=(area / SQ_METRES_PER_HECTARE) if area is not None else None,
        area_acres=(area / 4_046.8564224) if area is not None else None,
        perimeter_meters=row.perimeter_meters,
        length_meters=length,
        length_kilometers=(length / METRES_PER_KM) if length is not None else None,
        length_miles=(length / METRES_PER_MILE) if length is not None else None,
        vertex_count=row.vertex_count or 0,
        part_count=row.part_count or 1,
        is_valid=bool(row.is_valid),
        validity_reason=row.validity_reason,
        properties=row.attributes,
        measurement_note=row.measurement_note,
    )


# ══════════════════════════════════════════════════════════════════════════
#  Upload
# ══════════════════════════════════════════════════════════════════════════

@router.post(
    "/files/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a geospatial file",
    description=(
        "Accepts a `.zip` containing a Shapefile, or a `.kml` file. The file is "
        "stored and queued for background processing; poll "
        "`GET /api/files/{id}/` until the status reaches `COMPLETED`."
    ),
    responses={
        400: {"description": "Empty file or missing filename"},
        413: {"description": "File exceeds the configured size limit"},
        415: {"description": "Unsupported file extension"},
    },
)
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="A .zip Shapefile bundle or a .kml file"),
    db: AsyncSession = Depends(get_db),
) -> FileUploadResponse:
    """Accept an upload, persist it, and queue processing."""
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The upload is missing a filename.",
        )

    extension = Path(filename).suffix.lower()
    if extension not in settings.allowed_extensions_list:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {extension!r}. Allowed extensions: "
                f"{', '.join(settings.allowed_extensions_list)}."
            ),
        )

    file_type = "shapefile" if extension == ".zip" else "kml"
    file_id = str(uuid.uuid4())
    destination = settings.upload_path / f"{file_id}{extension}"

    size, content_hash = await _stream_upload_to_disk(file, destination)

    record = GeospatialFile(
        id=file_id,
        filename=filename,
        file_type=file_type,
        file_path=str(destination),
        file_size_bytes=size,
        content_hash=content_hash,
        status=FileStatus.PENDING,
    )
    db.add(record)
    await db.commit()

    # GeoPandas/GDAL work is CPU-bound and blocks the event loop, so the
    # pipeline runs in a worker thread rather than inline on the loop.
    import asyncio

    background_tasks.add_task(
        asyncio.to_thread, process_file_blocking, file_id, str(destination), file_type
    )

    logger.info("Accepted upload %s (%s, %d bytes)", file_id, file_type, size)

    return FileUploadResponse(
        id=file_id,
        filename=filename,
        file_type=file_type,
        file_size_bytes=size,
        status=FileStatus.PENDING.value,
        message="File accepted and queued for processing.",
        links={
            "self": f"/api/files/{file_id}/",
            "measurements": f"/api/files/{file_id}/measurements/",
            "geojson": f"/api/files/{file_id}/geojson/",
        },
    )


# ══════════════════════════════════════════════════════════════════════════
#  Reads
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/files/",
    response_model=FileListResponse,
    summary="List uploaded files",
    description="Returns a paginated list of uploads, newest first.",
)
async def list_files(
    skip: int = Query(0, ge=0, description="Records to skip"),
    limit: int = Query(20, ge=1, le=200, description="Maximum records to return"),
    file_status: Optional[Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED"]] = Query(
        None, alias="status", description="Filter by processing status"
    ),
    db: AsyncSession = Depends(get_db),
) -> FileListResponse:
    """Return one page of file records plus the true total."""
    filters = []
    if file_status:
        filters.append(GeospatialFile.status == FileStatus(file_status))

    # A real COUNT, so clients can page correctly rather than guessing from
    # the length of the current page.
    count_stmt = select(func.count()).select_from(GeospatialFile)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = select(GeospatialFile).order_by(GeospatialFile.uploaded_at.desc())
    if filters:
        stmt = stmt.where(*filters)
    records = (await db.execute(stmt.offset(skip).limit(limit))).scalars().all()

    return FileListResponse(
        total=total,
        skip=skip,
        limit=limit,
        count=len(records),
        files=[_file_info(r) for r in records],
    )


@router.get(
    "/files/{file_id}/",
    response_model=FileInfoResponse,
    summary="Get file information",
    responses={404: {"description": "No such file"}},
)
async def get_file_info(
    file_id: str, db: AsyncSession = Depends(get_db)
) -> FileInfoResponse:
    """Return status, CRS details and result counts for one file."""
    return _file_info(await _get_file_or_404(file_id, db))


@router.get(
    "/files/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get feature measurements",
    description=(
        "Returns per-feature geometry, attributes and measurements, with an "
        "aggregate summary. Available once processing has completed."
    ),
    responses={
        404: {"description": "No such file"},
        409: {"description": "File is still processing"},
        422: {"description": "Processing failed"},
    },
)
async def get_measurements(
    file_id: str,
    skip: int = Query(0, ge=0, description="Features to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum features to return"),
    geometry_type: Optional[str] = Query(
        None, description="Filter to one geometry type, e.g. Polygon"
    ),
    measurable_only: bool = Query(
        False, description="Return only features that produced a measurement"
    ),
    include_geometry: bool = Query(
        True, description="Set false to omit WKT/GeoJSON and shrink the payload"
    ),
    db: AsyncSession = Depends(get_db),
) -> MeasurementsResponse:
    """Return a page of measurements for one processed file."""
    record = await _get_file_or_404(file_id, db)
    _ensure_readable(record)

    stmt = select(FeatureMeasurement).where(FeatureMeasurement.file_id == file_id)
    if geometry_type:
        stmt = stmt.where(FeatureMeasurement.geometry_type == geometry_type)
    if measurable_only:
        stmt = stmt.where(FeatureMeasurement.is_measurable.is_(True))

    rows = (
        await db.execute(
            stmt.order_by(FeatureMeasurement.feature_index).offset(skip).limit(limit)
        )
    ).scalars().all()

    items = []
    for row in rows:
        item = _measurement_item(row)
        if not include_geometry:
            item.geometry_wkt = None
            item.geometry_geojson = None
        items.append(item)

    return MeasurementsResponse(
        file_id=file_id,
        filename=record.filename,
        crs=record.crs,
        crs_details=_crs_info(record),
        feature_count=record.feature_count or 0,
        summary=_summary(record),
        skip=skip,
        limit=limit,
        count=len(items),
        measurements=items,
    )


@router.get(
    "/files/{file_id}/geojson/",
    summary="Export measurements as GeoJSON",
    description=(
        "Returns a GeoJSON FeatureCollection in the source CRS, with each "
        "feature's measurements merged into its properties. Loads directly "
        "into QGIS, Leaflet, Mapbox or geojson.io."
    ),
    responses={
        200: {"content": {"application/geo+json": {}}},
        404: {"description": "No such file"},
        409: {"description": "File is still processing"},
    },
)
async def export_geojson(
    file_id: str,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serialise a processed file as a GeoJSON FeatureCollection."""
    record = await _get_file_or_404(file_id, db)
    _ensure_readable(record)

    rows = (
        await db.execute(
            select(FeatureMeasurement)
            .where(FeatureMeasurement.file_id == file_id)
            .order_by(FeatureMeasurement.feature_index)
        )
    ).scalars().all()

    features = []
    for row in rows:
        geometry = None
        if row.geometry_geojson:
            try:
                geometry = json.loads(row.geometry_geojson)
            except json.JSONDecodeError:
                geometry = None

        properties = dict(row.attributes)
        properties["_measurements"] = {
            "area_sq_meters": row.area_sq_meters,
            "perimeter_meters": row.perimeter_meters,
            "length_meters": row.length_meters,
            "projected_crs": row.projected_crs,
            "is_valid": bool(row.is_valid),
            "note": row.measurement_note,
        }

        features.append(
            {
                "type": "Feature",
                "id": row.feature_id or row.feature_index,
                "geometry": geometry,
                "properties": properties,
            }
        )

    collection = {
        "type": "FeatureCollection",
        "name": record.filename,
        "crs": {
            "type": "name",
            "properties": {"name": record.crs or "EPSG:4326"},
        },
        "features": features,
    }

    return Response(
        content=json.dumps(collection),
        media_type="application/geo+json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{Path(record.filename).stem}.geojson"'
            )
        },
    )


@router.delete(
    "/files/{file_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a file",
    description="Removes the stored upload, its database record and all measurements.",
    responses={404: {"description": "No such file"}},
)
async def delete_file(file_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    """Delete a file record, its measurements and the file on disk."""
    record = await _get_file_or_404(file_id, db)

    stored = Path(record.file_path)
    if stored.exists():
        try:
            stored.unlink()
        except OSError as exc:
            # The database record is still worth removing; a leftover blob is
            # recoverable by a janitor job, a stale row is more confusing.
            logger.warning("Could not delete %s: %s", stored, exc)

    await db.delete(record)
    await db.commit()
    logger.info("Deleted file %s", file_id)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════════════════
#  Operational endpoints
# ══════════════════════════════════════════════════════════════════════════

@router.get(
    "/health/",
    response_model=HealthResponse,
    summary="Health check",
    description="Verifies database connectivity and upload-directory writability.",
)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    """Report service and dependency health."""
    database = "ok"
    try:
        await db.execute(select(1))
    except Exception as exc:  # pragma: no cover - only on a real outage
        database = f"error: {type(exc).__name__}"
        logger.error("Health check database probe failed: %s", exc)

    writable = False
    try:
        probe = settings.upload_path / ".health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        logger.error("Upload directory is not writable: %s", settings.upload_path)

    return HealthResponse(
        status="ok" if database == "ok" and writable else "degraded",
        version=settings.app_version,
        database=database,
        upload_dir_writable=writable,
        supported_formats=settings.allowed_extensions_list,
    )


@router.get(
    "/stats/",
    response_model=StatsResponse,
    summary="Service statistics",
    description="Aggregate counts and totals across every file processed.",
)
async def stats(db: AsyncSession = Depends(get_db)) -> StatsResponse:
    """Return service-wide processing statistics."""
    total_files = (
        await db.execute(select(func.count()).select_from(GeospatialFile))
    ).scalar_one()

    by_status = {
        row[0].value if hasattr(row[0], "value") else str(row[0]): row[1]
        for row in (
            await db.execute(
                select(GeospatialFile.status, func.count()).group_by(
                    GeospatialFile.status
                )
            )
        ).all()
    }

    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(GeospatialFile.feature_count), 0),
                func.coalesce(func.sum(GeospatialFile.total_area_sq_meters), 0.0),
                func.coalesce(func.sum(GeospatialFile.total_length_meters), 0.0),
                func.avg(GeospatialFile.processing_ms),
            ).where(GeospatialFile.status == FileStatus.COMPLETED)
        )
    ).one()

    features, area, length, avg_ms = totals

    return StatsResponse(
        total_files=total_files,
        by_status=by_status,
        total_features=int(features or 0),
        total_area_sq_kilometers=float(area or 0.0) / SQ_METRES_PER_SQ_KM,
        total_length_kilometers=float(length or 0.0) / METRES_PER_KM,
        average_processing_ms=float(avg_ms) if avg_ms is not None else None,
    )
