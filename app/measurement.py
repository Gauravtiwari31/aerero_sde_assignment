"""
Measurement Engine
==================
Pure geometry measurement logic, free of database and framework concerns.

Every function here takes Shapely geometries that are **already in a projected,
metre-based CRS** (see :mod:`app.crs`) and returns plain data. Keeping this
layer pure makes the numeric behaviour directly unit-testable without spinning
up an app, a database, or a file on disk.

Supported measurements
----------------------
=================  ==========================================
Geometry           Measurement
=================  ==========================================
Polygon            area, perimeter
MultiPolygon       area, perimeter (summed over parts)
LineString         length
MultiLineString    length (summed over parts)
LinearRing         length
Point / MultiPoint none — reported explicitly, not as an error
GeometryCollection recursive, per-part aggregation
=================  ==========================================

Anything else is reported as unsupported with a note, never raised, so a single
odd feature cannot fail an otherwise valid upload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry.base import BaseGeometry

#: Geometry types for which an area is meaningful.
AREAL_TYPES = frozenset({"Polygon", "MultiPolygon"})

#: Geometry types for which a length is meaningful.
LINEAR_TYPES = frozenset({"LineString", "MultiLineString", "LinearRing"})

#: Geometry types that are valid but carry no measurement.
PUNCTUAL_TYPES = frozenset({"Point", "MultiPoint"})

#: Conversion factors from square metres.
SQ_METRES_PER_SQ_KM = 1_000_000
SQ_METRES_PER_HECTARE = 10_000
SQ_METRES_PER_ACRE = 4_046.8564224

#: Conversion factors from metres.
METRES_PER_KM = 1_000
METRES_PER_MILE = 1_609.344


@dataclass
class Measurement:
    """The measurement outcome for a single geometry.

    Attributes:
        geometry_type:  OGC type name of the measured geometry.
        is_supported:   Whether a measurement was computed. Points are valid but
                        unsupported; this flag distinguishes "nothing to
                        measure" from "could not measure".
        area_sq_meters: Area in m², for areal geometries only.
        perimeter_meters: Boundary length in metres, for areal geometries only.
        length_meters:  Length in metres, for linear geometries only.
        vertex_count:   Number of coordinate pairs, a useful complexity signal.
        part_count:     Number of constituent parts for multi-part geometries.
        is_valid:       Shapely validity. Self-intersecting polygons produce
                        unreliable areas, so this is surfaced to the caller.
        validity_reason: Explanation when ``is_valid`` is False.
        note:           Human-readable remark (e.g. why no measurement exists).
    """

    geometry_type: str
    is_supported: bool = False
    area_sq_meters: float | None = None
    perimeter_meters: float | None = None
    length_meters: float | None = None
    vertex_count: int = 0
    part_count: int = 1
    is_valid: bool = True
    validity_reason: str | None = None
    note: str | None = None

    def to_units(self) -> dict[str, Any]:
        """Expand the raw metre values into a multi-unit dictionary.

        Returns:
            A dict of derived units, omitting keys whose base value is None.
            Offering several units saves every client from re-deriving them and
            from the rounding drift that follows.
        """
        out: dict[str, Any] = {}
        if self.area_sq_meters is not None:
            out["area_sq_meters"] = self.area_sq_meters
            out["area_sq_kilometers"] = self.area_sq_meters / SQ_METRES_PER_SQ_KM
            out["area_hectares"] = self.area_sq_meters / SQ_METRES_PER_HECTARE
            out["area_acres"] = self.area_sq_meters / SQ_METRES_PER_ACRE
        if self.perimeter_meters is not None:
            out["perimeter_meters"] = self.perimeter_meters
        if self.length_meters is not None:
            out["length_meters"] = self.length_meters
            out["length_kilometers"] = self.length_meters / METRES_PER_KM
            out["length_miles"] = self.length_meters / METRES_PER_MILE
        return out


def count_vertices(geom: BaseGeometry) -> int:
    """Return the total number of coordinate pairs in ``geom``.

    Handles multi-part and nested geometries recursively, since Shapely only
    exposes ``.coords`` on single-part geometries.

    Args:
        geom: Any Shapely geometry.

    Returns:
        Total vertex count, or 0 if the geometry is empty or unreadable.
    """
    if geom is None or geom.is_empty:
        return 0
    if hasattr(geom, "geoms"):
        return sum(count_vertices(part) for part in geom.geoms)
    if geom.geom_type == "Polygon":
        total = len(geom.exterior.coords)
        total += sum(len(ring.coords) for ring in geom.interiors)
        return total
    try:
        return len(geom.coords)
    except (AttributeError, NotImplementedError):
        return 0


def measure(geom: BaseGeometry | None) -> Measurement:
    """Measure a single projected geometry.

    This function never raises for an unusual geometry — the spec requires that
    unsupported types degrade gracefully rather than crash the request. Failures
    are returned as an unsupported :class:`Measurement` carrying a note.

    Args:
        geom: A Shapely geometry already projected to a metre-based CRS, or
            None for a feature with null geometry.

    Returns:
        A populated :class:`Measurement`.
    """
    if geom is None:
        return Measurement(
            geometry_type="None",
            note="Feature has no geometry; nothing to measure.",
        )

    geom_type = geom.geom_type

    if geom.is_empty:
        return Measurement(
            geometry_type=geom_type,
            note=f"{geom_type} geometry is empty; nothing to measure.",
        )

    part_count = len(geom.geoms) if hasattr(geom, "geoms") else 1
    vertices = count_vertices(geom)

    # Validity is reported but never fatal: an invalid polygon still has a
    # computable area, and the caller may legitimately want it.
    is_valid = True
    validity_reason: str | None = None
    if geom_type in AREAL_TYPES:
        try:
            is_valid = geom.is_valid
            if not is_valid:
                from shapely.validation import explain_validity

                validity_reason = explain_validity(geom)
        except Exception:  # pragma: no cover - defensive
            is_valid = True

    base = {
        "geometry_type": geom_type,
        "vertex_count": vertices,
        "part_count": part_count,
        "is_valid": is_valid,
        "validity_reason": validity_reason,
    }

    try:
        if geom_type in AREAL_TYPES:
            return Measurement(
                is_supported=True,
                area_sq_meters=float(geom.area),
                perimeter_meters=float(geom.length),
                note=(
                    "Area computed on an invalid (self-intersecting) polygon; "
                    "treat the value as approximate."
                    if not is_valid
                    else None
                ),
                **base,
            )

        if geom_type in LINEAR_TYPES:
            return Measurement(
                is_supported=True,
                length_meters=float(geom.length),
                **base,
            )

        if geom_type in PUNCTUAL_TYPES:
            return Measurement(
                is_supported=True,
                note=f"No measurement is defined for {geom_type} geometries.",
                **base,
            )

        if geom_type == "GeometryCollection":
            return _measure_collection(geom, base)

        return Measurement(
            note=(
                f"Geometry type {geom_type!r} is not supported for measurement; "
                "the feature was still extracted and returned."
            ),
            **base,
        )

    except Exception as exc:  # pragma: no cover - defensive
        return Measurement(
            note=f"Measurement failed for {geom_type}: {exc}",
            **base,
        )


def _measure_collection(geom: BaseGeometry, base: dict[str, Any]) -> Measurement:
    """Aggregate measurements across the parts of a GeometryCollection.

    A collection may mix points, lines and polygons. Areas and lengths are
    summed independently so a mixed collection reports both.

    Args:
        geom: A Shapely GeometryCollection.
        base: Pre-computed shared fields to merge into the result.

    Returns:
        A :class:`Measurement` with summed area and/or length.
    """
    total_area = 0.0
    total_length = 0.0
    total_perimeter = 0.0
    saw_area = False
    saw_length = False

    for part in geom.geoms:
        sub = measure(part)
        if sub.area_sq_meters is not None:
            total_area += sub.area_sq_meters
            saw_area = True
        if sub.perimeter_meters is not None:
            total_perimeter += sub.perimeter_meters
        if sub.length_meters is not None:
            total_length += sub.length_meters
            saw_length = True

    kinds = sorted({p.geom_type for p in geom.geoms})
    return Measurement(
        is_supported=saw_area or saw_length,
        area_sq_meters=total_area if saw_area else None,
        perimeter_meters=total_perimeter if saw_area else None,
        length_meters=total_length if saw_length else None,
        note=(
            "GeometryCollection measured by summing its parts "
            f"({', '.join(kinds)})."
        ),
        **base,
    )


@dataclass
class DatasetSummary:
    """Aggregate statistics across every feature in one dataset.

    Computed once at processing time so the summary endpoint is a cheap read
    rather than a scan over every stored feature.
    """

    total_features: int = 0
    measured_features: int = 0
    unsupported_features: int = 0
    invalid_geometries: int = 0
    total_area_sq_meters: float = 0.0
    total_length_meters: float = 0.0
    geometry_type_counts: dict[str, int] = field(default_factory=dict)

    def add(self, m: Measurement) -> None:
        """Fold one feature's measurement into the running totals."""
        self.total_features += 1
        self.geometry_type_counts[m.geometry_type] = (
            self.geometry_type_counts.get(m.geometry_type, 0) + 1
        )
        if m.area_sq_meters is not None:
            self.total_area_sq_meters += m.area_sq_meters
        if m.length_meters is not None:
            self.total_length_meters += m.length_meters
        if m.area_sq_meters is not None or m.length_meters is not None:
            self.measured_features += 1
        elif not m.is_supported:
            self.unsupported_features += 1
        if not m.is_valid:
            self.invalid_geometries += 1
