"""
Measurement Engine Tests
========================
Unit tests for :mod:`app.measurement`, using geometries whose area and length
are known analytically so the assertions verify real correctness rather than
pinning whatever the implementation currently returns.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    LinearRing,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

from app.measurement import DatasetSummary, count_vertices, measure


# ── Polygons ─────────────────────────────────────────────────────────────

def test_square_polygon_area_and_perimeter():
    """A 100x100 square has area 10,000 and perimeter 400."""
    m = measure(Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]))

    assert m.is_supported is True
    assert m.area_sq_meters == pytest.approx(10_000.0)
    assert m.perimeter_meters == pytest.approx(400.0)
    assert m.length_meters is None, "Polygons must not report a length"
    assert m.is_valid is True


def test_polygon_with_hole_subtracts_hole_area():
    """A 10x10 square with a 2x2 hole has area 100 - 4 = 96."""
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
    m = measure(Polygon(outer, [hole]))

    assert m.area_sq_meters == pytest.approx(96.0)


def test_multipolygon_sums_part_areas():
    """A MultiPolygon reports the sum of its parts' areas."""
    a = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    b = Polygon([(20, 20), (25, 20), (25, 25), (20, 25)])
    m = measure(MultiPolygon([a, b]))

    assert m.area_sq_meters == pytest.approx(100.0 + 25.0)
    assert m.part_count == 2


def test_self_intersecting_polygon_is_flagged_but_still_measured():
    """An invalid bowtie polygon is reported, not rejected."""
    m = measure(Polygon([(0, 0), (10, 10), (10, 0), (0, 10)]))

    assert m.is_valid is False
    assert m.validity_reason is not None
    assert "Self-intersection" in m.validity_reason
    assert m.area_sq_meters is not None, "An area is still computed"
    assert "approximate" in (m.note or "")


# ── Lines ────────────────────────────────────────────────────────────────

def test_linestring_length_matches_pythagoras():
    """A 3-4-5 triangle hypotenuse scaled by 100 measures 500."""
    m = measure(LineString([(0, 0), (300, 400)]))

    assert m.is_supported is True
    assert m.length_meters == pytest.approx(500.0)
    assert m.area_sq_meters is None, "Lines must not report an area"


def test_multilinestring_sums_part_lengths():
    """A MultiLineString reports the sum of its parts' lengths."""
    m = measure(
        MultiLineString([[(0, 0), (10, 0)], [(0, 5), (0, 25)]])
    )

    assert m.length_meters == pytest.approx(30.0)
    assert m.part_count == 2


def test_linear_ring_is_measured_as_length():
    """A LinearRing is linear, so it yields a length, not an area."""
    m = measure(LinearRing([(0, 0), (10, 0), (10, 10), (0, 10)]))

    assert m.length_meters == pytest.approx(40.0)
    assert m.area_sq_meters is None


# ── Points ───────────────────────────────────────────────────────────────

def test_point_is_supported_but_has_no_measurement():
    """Points are valid input with no measurement, per the specification."""
    m = measure(Point(5, 5))

    assert m.is_supported is True, "A Point is handled, not rejected"
    assert m.area_sq_meters is None
    assert m.length_meters is None
    assert "No measurement is defined" in m.note


def test_multipoint_has_no_measurement():
    """MultiPoint behaves like Point."""
    m = measure(MultiPoint([(0, 0), (1, 1)]))

    assert m.area_sq_meters is None
    assert m.length_meters is None


# ── Edge cases ───────────────────────────────────────────────────────────

def test_none_geometry_degrades_gracefully():
    """A null geometry returns a note rather than raising."""
    m = measure(None)

    assert m.is_supported is False
    assert m.geometry_type == "None"
    assert "no geometry" in m.note.lower()


def test_empty_geometry_degrades_gracefully():
    """An empty geometry returns a note rather than raising."""
    m = measure(Polygon())

    assert m.is_supported is False
    assert "empty" in m.note.lower()


def test_geometry_collection_aggregates_mixed_parts():
    """A collection sums areas and lengths independently."""
    m = measure(
        GeometryCollection(
            [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                LineString([(0, 0), (50, 0)]),
                Point(1, 1),
            ]
        )
    )

    assert m.area_sq_meters == pytest.approx(100.0)
    assert m.length_meters == pytest.approx(50.0)
    assert m.is_supported is True


# ── Unit conversion ──────────────────────────────────────────────────────

def test_unit_conversions_are_internally_consistent():
    """Derived units agree with their defining ratios."""
    m = measure(Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]))
    units = m.to_units()

    assert units["area_sq_meters"] == pytest.approx(1_000_000.0)
    assert units["area_sq_kilometers"] == pytest.approx(1.0)
    assert units["area_hectares"] == pytest.approx(100.0)
    assert units["area_acres"] == pytest.approx(247.105, rel=1e-3)


def test_length_unit_conversions():
    """One mile in metres converts back to one mile."""
    m = measure(LineString([(0, 0), (1609.344, 0)]))
    units = m.to_units()

    assert units["length_kilometers"] == pytest.approx(1.609344)
    assert units["length_miles"] == pytest.approx(1.0)


def test_point_units_are_empty():
    """A Point produces no unit entries at all."""
    assert measure(Point(0, 0)).to_units() == {}


# ── Vertex counting ──────────────────────────────────────────────────────

def test_vertex_count_includes_closing_coordinate():
    """Shapely closes polygon rings, so a square has 5 exterior coordinates."""
    assert count_vertices(Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])) == 5


def test_vertex_count_includes_interior_rings():
    """Holes contribute their vertices to the total."""
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole = [(4, 4), (6, 4), (6, 6), (4, 6)]
    assert count_vertices(Polygon(outer, [hole])) == 10


def test_vertex_count_recurses_into_multipart():
    """Multi-part geometries sum their parts' vertices."""
    assert count_vertices(MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])) == 4


def test_vertex_count_of_empty_is_zero():
    """An empty geometry has no vertices."""
    assert count_vertices(Polygon()) == 0


# ── Dataset summary ──────────────────────────────────────────────────────

def test_dataset_summary_accumulates_correctly():
    """The summary folds mixed features into accurate totals."""
    summary = DatasetSummary()
    summary.add(measure(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])))
    summary.add(measure(LineString([(0, 0), (100, 0)])))
    summary.add(measure(Point(1, 1)))

    assert summary.total_features == 3
    assert summary.measured_features == 2, "The Point is not a measured feature"
    assert summary.total_area_sq_meters == pytest.approx(100.0)
    assert summary.total_length_meters == pytest.approx(100.0)
    assert summary.geometry_type_counts == {
        "Polygon": 1,
        "LineString": 1,
        "Point": 1,
    }


def test_dataset_summary_counts_invalid_geometries():
    """Invalid geometries are tallied separately."""
    summary = DatasetSummary()
    summary.add(measure(Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])))

    assert summary.invalid_geometries == 1
