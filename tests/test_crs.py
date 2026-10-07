"""
CRS Resolution Tests
====================
Verifies requirement 5: measurements must never be computed on geographic
(degree-based) coordinates.

The decisive test is :func:`test_degree_based_area_is_catastrophically_wrong`,
which demonstrates the magnitude of the bug being prevented — measuring in
degrees understates a 1 km² parcel by roughly ten orders of magnitude.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from app.crs import (
    DEFAULT_ASSUMED_CRS,
    resolve_measurement_crs,
    to_authority_code,
)

# Bengaluru — unambiguously inside UTM zone 43N (EPSG:32643).
REF_LON, REF_LAT = 77.5946, 12.9716


def _wgs84_frame(geometry) -> gpd.GeoDataFrame:
    """Wrap a geometry in a WGS84 GeoDataFrame."""
    return gpd.GeoDataFrame({"id": [1]}, geometry=[geometry], crs="EPSG:4326")


# ── Authority-code normalisation ─────────────────────────────────────────

def test_authority_code_is_compact():
    """A CRS renders as EPSG:CODE, matching the specification's example."""
    assert to_authority_code(CRS.from_epsg(4326)) == "EPSG:4326"
    assert to_authority_code("EPSG:32643") == "EPSG:32643"


def test_authority_code_handles_none():
    """A missing CRS renders as UNKNOWN rather than raising."""
    assert to_authority_code(None) == "UNKNOWN"


# ── Reprojection behaviour ───────────────────────────────────────────────

def test_geographic_input_is_reprojected_to_utm():
    """WGS84 input is reprojected to the UTM zone covering the data."""
    gdf = _wgs84_frame(Point(REF_LON, REF_LAT))
    projected, decision = resolve_measurement_crs(gdf)

    assert decision.source_crs == "EPSG:4326"
    assert decision.strategy == "utm_centroid"
    assert decision.projected_crs == "EPSG:32643", "Bengaluru is in UTM zone 43N"
    assert projected.crs.is_projected is True
    assert decision.rationale, "The decision must be explainable"


def test_projected_metre_input_is_left_alone():
    """Already-projected metre-based data is measured in place."""
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])],
        crs="EPSG:32643",
    )
    projected, decision = resolve_measurement_crs(gdf)

    assert decision.strategy == "source_projected"
    assert decision.projected_crs == "EPSG:32643"
    assert projected.crs == gdf.crs, "No needless reprojection"


def test_missing_crs_is_assumed_wgs84():
    """A dataset with no CRS is assumed to be WGS84 and flagged as such."""
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(REF_LON, REF_LAT)])
    assert gdf.crs is None

    _, decision = resolve_measurement_crs(gdf)

    assert decision.assumed_source is True
    assert decision.source_crs == DEFAULT_ASSUMED_CRS
    assert decision.strategy == "utm_centroid"


def test_row_order_is_preserved_through_reprojection():
    """Reprojection must not reorder rows, since callers pair frames by position."""
    gdf = gpd.GeoDataFrame(
        {"id": [10, 20, 30]},
        geometry=[
            Point(REF_LON, REF_LAT),
            Point(REF_LON + 0.01, REF_LAT),
            Point(REF_LON + 0.02, REF_LAT),
        ],
        crs="EPSG:4326",
    )
    projected, _ = resolve_measurement_crs(gdf)

    assert list(projected["id"]) == [10, 20, 30]
    assert list(projected.index) == list(gdf.index)


# ── The core correctness guarantee ───────────────────────────────────────

def test_degree_based_area_is_catastrophically_wrong():
    """Demonstrate why reprojection is mandatory, not optional.

    A parcel of roughly 1 km² measured in raw degrees yields a number ~10
    orders of magnitude too small. This test documents the failure mode that
    requirement 5 exists to prevent.
    """
    import math

    side_m = 1000.0
    d_lat = side_m / 110_574.0
    d_lon = side_m / (111_320.0 * math.cos(math.radians(REF_LAT)))

    parcel = Polygon(
        [
            (REF_LON, REF_LAT),
            (REF_LON + d_lon, REF_LAT),
            (REF_LON + d_lon, REF_LAT + d_lat),
            (REF_LON, REF_LAT + d_lat),
        ]
    )

    naive_degree_area = parcel.area
    projected, _ = resolve_measurement_crs(_wgs84_frame(parcel))
    correct_area = projected.geometry.iloc[0].area

    assert correct_area == pytest.approx(1_000_000.0, rel=0.01), (
        "Projected area must match the ~1 km² ground truth within 1%"
    )
    assert naive_degree_area < 1e-3, "Degree-based area is a meaningless tiny number"
    assert correct_area / naive_degree_area > 1e9, (
        "The two differ by ~10 orders of magnitude, which is exactly the bug "
        "that reprojection prevents"
    )


def test_projected_length_matches_known_ground_distance():
    """One degree of latitude is ~110.6 km; the projected length should agree."""
    line = LineString([(REF_LON, REF_LAT), (REF_LON, REF_LAT + 1.0)])
    projected, _ = resolve_measurement_crs(_wgs84_frame(line))

    length_km = projected.geometry.iloc[0].length / 1000.0
    assert length_km == pytest.approx(110.6, rel=0.01)


def test_utm_zone_tracks_longitude():
    """Datasets in different hemispheres get different, correct UTM zones."""
    # New York City — UTM zone 18N.
    nyc = _wgs84_frame(Point(-74.0060, 40.7128))
    _, nyc_decision = resolve_measurement_crs(nyc)
    assert nyc_decision.projected_crs == "EPSG:32618"

    # Sydney — UTM zone 56S.
    sydney = _wgs84_frame(Point(151.2093, -33.8688))
    _, sydney_decision = resolve_measurement_crs(sydney)
    assert sydney_decision.projected_crs == "EPSG:32756"
