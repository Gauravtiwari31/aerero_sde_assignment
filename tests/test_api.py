"""
API Integration Tests
=====================
End-to-end tests that drive the real application over HTTP, through the real
GDAL read path, against a throwaway database.

These verify the contract described in the specification: upload, file
information, and measurements, plus the error behaviour around each.
"""

from __future__ import annotations

import pytest

from tests.fixtures import (
    REF_LAT,
    REF_LON,
    malformed_kml,
    mixed_geometry_kml,
    shapefile_zip,
    square_kml,
    zip_without_shapefile,
)

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════════
#  Upload validation
# ══════════════════════════════════════════════════════════════════════════

async def test_unsupported_extension_is_rejected(client):
    """A .txt upload is refused with 415 and a helpful message."""
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    response = await client.post("/api/files/", files=files)

    assert response.status_code == 415
    body = response.json()
    assert body["error"] == "UNSUPPORTED_FILE_TYPE"
    assert ".zip" in body["message"] and ".kml" in body["message"]


async def test_empty_upload_is_rejected(client):
    """A zero-byte file is refused rather than queued for a doomed parse."""
    files = {"file": ("empty.kml", b"", "application/vnd.google-earth.kml+xml")}
    response = await client.post("/api/files/", files=files)

    assert response.status_code == 400
    assert "empty" in response.json()["message"].lower()


async def test_oversized_upload_is_rejected(client, monkeypatch):
    """An upload beyond the configured cap is refused with 413."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_size_mb", 1)

    payload = b"\x00" * (2 * 1024 * 1024)  # 2 MB against a 1 MB cap
    files = {"file": ("big.kml", payload, "application/octet-stream")}
    response = await client.post("/api/files/", files=files)

    assert response.status_code == 413
    assert response.json()["error"] == "FILE_TOO_LARGE"


async def test_upload_response_contains_followup_links(client):
    """The acceptance payload tells the client where to look next."""
    files = {"file": ("p.kml", square_kml(), "application/octet-stream")}
    response = await client.post("/api/files/", files=files)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["file_type"] == "kml"
    assert body["file_size_bytes"] > 0
    assert body["links"]["self"].endswith(f"/api/files/{body['id']}/")
    assert "measurements" in body["links"]


# ══════════════════════════════════════════════════════════════════════════
#  KML processing
# ══════════════════════════════════════════════════════════════════════════

async def test_kml_polygon_area_matches_ground_truth(upload_and_wait, client):
    """A 1 km square KML parcel measures ~1,000,000 m² after reprojection."""
    _, info = await upload_and_wait("parcel.kml", square_kml(side_meters=1000.0))

    assert info["status"] == "COMPLETED", info.get("error_message")
    assert info["feature_count"] == 1
    assert info["crs"] == "EPSG:4326"

    measurements = (
        await client.get(f"/api/files/{info['id']}/measurements/")
    ).json()
    feature = measurements["measurements"][0]

    assert feature["geometry_type"] == "Polygon"
    assert feature["area_sq_meters"] == pytest.approx(1_000_000.0, rel=0.02)
    assert feature["area_hectares"] == pytest.approx(100.0, rel=0.02)
    assert feature["area_sq_kilometers"] == pytest.approx(1.0, rel=0.02)
    assert feature["projected_crs"] == "EPSG:32643", "Bengaluru is UTM zone 43N"
    assert feature["is_measurable"] is True


async def test_mixed_geometry_kml_measures_each_type_correctly(
    upload_and_wait, client
):
    """Polygon gets area, LineString gets length, Point gets neither."""
    _, info = await upload_and_wait("mixed.kml", mixed_geometry_kml())
    assert info["status"] == "COMPLETED", info.get("error_message")

    body = (await client.get(f"/api/files/{info['id']}/measurements/")).json()
    by_type = {m["geometry_type"]: m for m in body["measurements"]}

    assert "Polygon" in by_type
    polygon = by_type["Polygon"]
    assert polygon["area_sq_meters"] > 0
    assert polygon["perimeter_meters"] > 0
    assert polygon["length_meters"] is None, "A polygon reports no length"

    assert "LineString" in by_type
    line = by_type["LineString"]
    assert line["length_meters"] > 0
    assert line["area_sq_meters"] is None, "A line reports no area"

    assert "Point" in by_type
    point = by_type["Point"]
    assert point["area_sq_meters"] is None
    assert point["length_meters"] is None
    assert "No measurement" in point["measurement_note"]


async def test_measurement_summary_aggregates_the_dataset(upload_and_wait, client):
    """The summary block reports accurate per-type counts and totals."""
    _, info = await upload_and_wait("mixed.kml", mixed_geometry_kml())
    body = (await client.get(f"/api/files/{info['id']}/measurements/")).json()
    summary = body["summary"]

    assert summary["total_features"] == 3
    assert summary["measured_features"] == 2, "The Point is not measured"
    assert summary["total_area_sq_meters"] > 0
    assert summary["total_length_meters"] > 0
    assert summary["geometry_type_counts"]["Point"] == 1


async def test_crs_decision_is_explained(upload_and_wait, client):
    """The response explains which CRS was used for measurement and why."""
    _, info = await upload_and_wait("parcel.kml", square_kml())
    crs = info["crs_details"]

    assert crs["source"] == "EPSG:4326"
    assert crs["projected"] == "EPSG:32643"
    assert crs["strategy"] == "utm_centroid"
    assert "UTM" in crs["rationale"]
    assert crs["assumed"] is False


# ══════════════════════════════════════════════════════════════════════════
#  Shapefile processing
# ══════════════════════════════════════════════════════════════════════════

async def test_shapefile_zip_is_processed(upload_and_wait, client, tmp_path):
    """A zipped shapefile is read, measured and its attributes preserved."""
    _, info = await upload_and_wait("parcels.zip", shapefile_zip(tmp_path))

    assert info["status"] == "COMPLETED", info.get("error_message")
    assert info["file_type"] == "shapefile"
    assert info["feature_count"] == 2

    body = (await client.get(f"/api/files/{info['id']}/measurements/")).json()
    feature = body["measurements"][0]

    assert feature["geometry_type"] == "Polygon"
    assert feature["area_sq_meters"] > 0
    assert feature["properties"]["name"] == "Parcel A"
    assert feature["properties"]["owner"] == "Alice"
    assert feature["properties"]["parcel_no"] == 101


async def test_shapefile_without_prj_assumes_wgs84(
    upload_and_wait, client, tmp_path
):
    """A shapefile lacking .prj is processed with an assumed CRS, and says so."""
    payload = shapefile_zip(tmp_path, include_prj=False)
    _, info = await upload_and_wait("noprj.zip", payload)

    assert info["status"] == "COMPLETED", info.get("error_message")
    assert info["crs_details"]["assumed"] is True


async def test_line_shapefile_measures_length(upload_and_wait, client, tmp_path):
    """A line-geometry shapefile produces lengths, not areas."""
    payload = shapefile_zip(tmp_path, geometry_kind="line")
    _, info = await upload_and_wait("roads.zip", payload)

    assert info["status"] == "COMPLETED", info.get("error_message")
    body = (await client.get(f"/api/files/{info['id']}/measurements/")).json()

    for feature in body["measurements"]:
        assert feature["geometry_type"] == "LineString"
        assert feature["length_meters"] > 0
        assert feature["area_sq_meters"] is None


# ══════════════════════════════════════════════════════════════════════════
#  Graceful failure
# ══════════════════════════════════════════════════════════════════════════

async def test_zip_without_shapefile_fails_with_clear_reason(
    upload_and_wait, client
):
    """An archive with no .shp is marked FAILED with an actionable error."""
    _, info = await upload_and_wait("nodata.zip", zip_without_shapefile())

    assert info["status"] == "FAILED"
    assert info["error_code"] == "INVALID_SHAPEFILE"
    assert ".shp" in info["error_message"]


async def test_malformed_kml_fails_without_crashing(upload_and_wait):
    """Unparseable KML marks the file FAILED rather than raising a 500."""
    _, info = await upload_and_wait("broken.kml", malformed_kml())

    assert info["status"] == "FAILED"
    assert info["error_code"] in ("UNREADABLE_FILE", "EMPTY_DATASET")
    assert info["error_message"]


async def test_measurements_of_failed_file_return_422(upload_and_wait, client):
    """Reading measurements for a failed file explains the failure."""
    _, info = await upload_and_wait("nodata.zip", zip_without_shapefile())
    response = await client.get(f"/api/files/{info['id']}/measurements/")

    assert response.status_code == 422
    assert "INVALID_SHAPEFILE" in response.json()["message"]


async def test_unknown_file_id_returns_404(client):
    """An unknown id yields a 404 in the standard error envelope."""
    response = await client.get("/api/files/does-not-exist/")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "NOT_FOUND"
    assert "does-not-exist" in body["message"]


async def test_unknown_measurements_id_returns_404(client):
    """Measurements for an unknown id also 404."""
    response = await client.get("/api/files/does-not-exist/measurements/")
    assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
#  Listing, filtering and pagination
# ══════════════════════════════════════════════════════════════════════════

async def test_list_reports_a_true_total(upload_and_wait, client):
    """``total`` counts all matching records, not just the current page."""
    for i in range(3):
        await upload_and_wait(f"p{i}.kml", square_kml())

    response = await client.get("/api/files/?limit=2")
    body = response.json()

    assert body["count"] == 2, "The page holds the requested number"
    assert body["total"] >= 3, "The total counts beyond the page"
    assert body["limit"] == 2


async def test_list_can_filter_by_status(upload_and_wait, client):
    """Filtering by status returns only matching records."""
    await upload_and_wait("good.kml", square_kml())
    await upload_and_wait("bad.zip", zip_without_shapefile())

    completed = (await client.get("/api/files/?status=COMPLETED")).json()
    failed = (await client.get("/api/files/?status=FAILED")).json()

    assert all(f["status"] == "COMPLETED" for f in completed["files"])
    assert all(f["status"] == "FAILED" for f in failed["files"])
    assert failed["total"] >= 1


async def test_measurements_pagination_and_filters(upload_and_wait, client):
    """Measurements paginate and can be filtered by geometry type."""
    _, info = await upload_and_wait("mixed.kml", mixed_geometry_kml())
    file_id = info["id"]

    page = (await client.get(f"/api/files/{file_id}/measurements/?limit=1")).json()
    assert page["count"] == 1
    assert page["feature_count"] == 3, "The file still reports its full count"

    polygons = (
        await client.get(
            f"/api/files/{file_id}/measurements/?geometry_type=Polygon"
        )
    ).json()
    assert all(m["geometry_type"] == "Polygon" for m in polygons["measurements"])

    measurable = (
        await client.get(f"/api/files/{file_id}/measurements/?measurable_only=true")
    ).json()
    assert all(m["is_measurable"] for m in measurable["measurements"])
    assert measurable["count"] == 2, "The Point is excluded"


async def test_geometry_can_be_omitted_to_shrink_payload(upload_and_wait, client):
    """``include_geometry=false`` drops WKT and GeoJSON from the response."""
    _, info = await upload_and_wait("parcel.kml", square_kml())
    body = (
        await client.get(
            f"/api/files/{info['id']}/measurements/?include_geometry=false"
        )
    ).json()

    feature = body["measurements"][0]
    assert feature["geometry_wkt"] is None
    assert feature["geometry_geojson"] is None
    assert feature["area_sq_meters"] is not None, "Measurements are still present"


# ══════════════════════════════════════════════════════════════════════════
#  GeoJSON export
# ══════════════════════════════════════════════════════════════════════════

async def test_geojson_export_is_a_valid_feature_collection(
    upload_and_wait, client
):
    """The export is a well-formed FeatureCollection carrying measurements."""
    _, info = await upload_and_wait("mixed.kml", mixed_geometry_kml())
    response = await client.get(f"/api/files/{info['id']}/geojson/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/geo+json")

    collection = response.json()
    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 3

    feature = collection["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] in ("Polygon", "LineString", "Point")
    assert "_measurements" in feature["properties"]
    assert feature["properties"]["_measurements"]["projected_crs"] == "EPSG:32643"


async def test_geojson_export_sets_download_filename(upload_and_wait, client):
    """The export is served as a downloadable .geojson file."""
    _, info = await upload_and_wait("survey.kml", square_kml())
    response = await client.get(f"/api/files/{info['id']}/geojson/")

    assert "survey.geojson" in response.headers["content-disposition"]


# ══════════════════════════════════════════════════════════════════════════
#  Deletion
# ══════════════════════════════════════════════════════════════════════════

async def test_delete_removes_file_and_measurements(upload_and_wait, client):
    """Deleting a file removes it and makes subsequent reads 404."""
    _, info = await upload_and_wait("parcel.kml", square_kml())
    file_id = info["id"]

    assert (await client.delete(f"/api/files/{file_id}/")).status_code == 204
    assert (await client.get(f"/api/files/{file_id}/")).status_code == 404
    assert (
        await client.get(f"/api/files/{file_id}/measurements/")
    ).status_code == 404


async def test_delete_unknown_file_returns_404(client):
    """Deleting a non-existent file is a 404, not a silent success."""
    assert (await client.delete("/api/files/nope/")).status_code == 404


# ══════════════════════════════════════════════════════════════════════════
#  Operational endpoints
# ══════════════════════════════════════════════════════════════════════════

async def test_health_reports_dependencies(client):
    """Health confirms database connectivity and upload writability."""
    body = (await client.get("/api/health/")).json()

    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["upload_dir_writable"] is True
    assert ".kml" in body["supported_formats"]


async def test_stats_aggregate_across_files(upload_and_wait, client):
    """Statistics accumulate features and area across processed files."""
    await upload_and_wait("a.kml", square_kml(side_meters=1000.0))
    body = (await client.get("/api/stats/")).json()

    assert body["total_files"] >= 1
    assert body["total_features"] >= 1
    assert body["total_area_sq_kilometers"] > 0
    assert "COMPLETED" in body["by_status"]


# ══════════════════════════════════════════════════════════════════════════
#  Cross-cutting behaviour
# ══════════════════════════════════════════════════════════════════════════

async def test_every_response_carries_a_request_id(client):
    """Responses are traceable via X-Request-ID."""
    response = await client.get("/api/health/")

    assert response.headers.get("X-Request-ID")
    assert float(response.headers["X-Process-Time-Ms"]) >= 0


async def test_supplied_request_id_is_echoed(client):
    """A client-supplied correlation ID is preserved end to end."""
    response = await client.get("/api/health/", headers={"X-Request-ID": "trace-123"})

    assert response.headers["X-Request-ID"] == "trace-123"


async def test_openapi_schema_documents_every_endpoint(client):
    """The generated OpenAPI schema covers the documented surface."""
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]

    assert "/api/files/" in paths
    assert "/api/files/{file_id}/" in paths
    assert "/api/files/{file_id}/measurements/" in paths
    assert "/api/files/{file_id}/geojson/" in paths
    assert "post" in paths["/api/files/"]


async def test_invalid_pagination_is_rejected(client):
    """Out-of-range pagination parameters are refused with a clear error."""
    response = await client.get("/api/files/?limit=9999")

    assert response.status_code == 422
    assert response.json()["error"] == "VALIDATION_ERROR"
