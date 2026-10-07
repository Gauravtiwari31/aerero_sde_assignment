"""
CRS Resolution Strategy
=======================
Decides which projected coordinate reference system to use when measuring a
dataset, and explains *why* it was chosen.

Why this matters
----------------
Area and length are meaningless when computed directly on geographic
coordinates: a "degree" is not a unit of distance, and its ground distance
varies with latitude. Every measurement in this service is therefore computed
on a **projected** CRS whose units are metres.

Selection strategy (in priority order)
--------------------------------------
1. **Already projected** — if the source CRS is projected and metre-based, it is
   used as-is. Reprojecting would only add resampling error.
2. **UTM zone from the data's centroid** — for geographic data, the UTM zone
   containing the dataset's centre gives sub-metre accuracy over the zone and is
   the standard choice for survey-scale work.
3. **Configured fallback** — if UTM estimation fails (e.g. an exotic datum that
   pyproj cannot resolve), the configured default is used.

UTM is preferred over a global projection such as Web Mercator (EPSG:3857)
because Web Mercator's area distortion grows with the secant of latitude —
roughly 2x at 45 degrees and far worse towards the poles — which makes it
unsuitable for measurement even though it is ubiquitous for tiles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import geopandas as gpd
from pyproj import CRS

logger = logging.getLogger(__name__)

#: CRS assumed when a dataset declares none. WGS84 is the near-universal
#: default for KML (the format mandates it) and for undeclared shapefiles.
DEFAULT_ASSUMED_CRS = "EPSG:4326"


@dataclass(frozen=True)
class CRSDecision:
    """The outcome of CRS resolution for one dataset.

    Attributes:
        source_crs:      Authority code of the data as supplied (e.g. ``EPSG:4326``).
        projected_crs:   Authority code actually used for measurement.
        strategy:        Which rule fired — ``source_projected``, ``utm_centroid``
                         or ``configured_fallback``.
        rationale:       Human-readable explanation, surfaced in the API so the
                         measurement is auditable.
        assumed_source:  True when the file declared no CRS and WGS84 was assumed.
    """

    source_crs: str
    projected_crs: str
    strategy: str
    rationale: str
    assumed_source: bool = False


def to_authority_code(crs: CRS | str | None) -> str:
    """Render a CRS as a compact ``AUTHORITY:CODE`` string.

    The spec's example response shows ``"crs": "EPSG:4326"`` rather than a
    multi-line WKT blob, so we normalise to the authority code wherever pyproj
    can identify one and fall back to the CRS name otherwise.

    Args:
        crs: A pyproj CRS, a CRS-like string, or None.

    Returns:
        A string such as ``"EPSG:32643"``, the CRS name if no authority code
        can be derived, or ``"UNKNOWN"`` for None.
    """
    if crs is None:
        return "UNKNOWN"
    try:
        parsed = crs if isinstance(crs, CRS) else CRS.from_user_input(crs)
    except Exception:
        return str(crs)

    code = parsed.to_authority()
    if code:
        return f"{code[0]}:{code[1]}"
    return parsed.name or str(crs)


def _is_metre_based(crs: CRS) -> bool:
    """Return True when every axis of ``crs`` is measured in metres."""
    try:
        return all(
            axis.unit_name in ("metre", "meter") for axis in crs.axis_info
        )
    except Exception:
        return False


def resolve_measurement_crs(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, CRSDecision]:
    """Return a metre-based copy of ``gdf`` plus a record of how it was chosen.

    The returned GeoDataFrame preserves row order and index, so callers can zip
    it against the original frame to pair each source geometry with its
    projected counterpart.

    Args:
        gdf: The dataset as read from disk. May have no CRS set.

    Returns:
        ``(projected_gdf, decision)``.
    """
    assumed = False

    # ── Step 1: ensure the frame has a CRS at all ────────────────────────
    if gdf.crs is None:
        logger.warning("Dataset declares no CRS; assuming %s", DEFAULT_ASSUMED_CRS)
        gdf = gdf.set_crs(DEFAULT_ASSUMED_CRS, allow_override=True)
        assumed = True

    source_code = to_authority_code(gdf.crs)

    # ── Step 2: already projected and metre-based → measure in place ─────
    if gdf.crs.is_projected and _is_metre_based(gdf.crs):
        return gdf, CRSDecision(
            source_crs=source_code,
            projected_crs=source_code,
            strategy="source_projected",
            rationale=(
                f"Source CRS {source_code} is already projected with metre units; "
                "measured in place to avoid reprojection error."
            ),
            assumed_source=assumed,
        )

    # ── Step 3: estimate the UTM zone covering the data ──────────────────
    try:
        utm = gdf.estimate_utm_crs()
        projected = gdf.to_crs(utm)
        utm_code = to_authority_code(utm)
        return projected, CRSDecision(
            source_crs=source_code,
            projected_crs=utm_code,
            strategy="utm_centroid",
            rationale=(
                f"Source CRS {source_code} uses angular units, so measurement "
                f"would be invalid. Reprojected to {utm_code}, the UTM zone "
                "covering the dataset's centroid, which preserves distance and "
                "area to sub-metre accuracy across the zone."
            ),
            assumed_source=assumed,
        )
    except Exception as exc:  # pragma: no cover - exercised only on exotic datums
        logger.warning("UTM estimation failed (%s); using configured fallback", exc)

    # ── Step 4: configured fallback ──────────────────────────────────────
    from app.config import get_settings

    fallback = get_settings().default_projected_crs
    projected = gdf.to_crs(fallback)
    fallback_code = to_authority_code(fallback)
    return projected, CRSDecision(
        source_crs=source_code,
        projected_crs=fallback_code,
        strategy="configured_fallback",
        rationale=(
            f"UTM zone estimation failed for source CRS {source_code}; fell back "
            f"to the configured projected CRS {fallback_code}. Measurements may "
            "carry projection distortion that grows with distance from the "
            "projection's standard parallel."
        ),
        assumed_source=assumed,
    )
