"""
Test Fixture Builders
=====================
Generates real geospatial files in-memory so the suite exercises the actual
GDAL/Fiona read path rather than mocking it.

Fixtures are built around coordinates with *independently known* ground truth,
so assertions check real correctness rather than merely re-asserting whatever
the code happens to produce. See :func:`square_kml` for the worked example.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

#: A reference location in Bengaluru, India — UTM zone 43N (EPSG:32643).
#: Chosen because it sits comfortably inside a single UTM zone, so the
#: centroid-based zone estimate is unambiguous.
REF_LON = 77.5946
REF_LAT = 12.9716


def square_kml(side_meters: float = 1000.0) -> bytes:
    """Build a KML holding one near-square polygon of a known ground size.

    At latitude ``REF_LAT`` one degree of latitude spans ~110,574 m and one
    degree of longitude spans ~111,320·cos(lat) m. Converting a metre-sized
    side into degrees with those factors gives a polygon whose projected area
    should land within ~1% of ``side_meters**2`` — a tolerance that comfortably
    absorbs the spherical-vs-ellipsoidal approximation while still failing
    loudly if measurement happens in degrees (which would be off by ~10 orders
    of magnitude).

    Args:
        side_meters: Desired side length on the ground, in metres.

    Returns:
        UTF-8 encoded KML bytes.
    """
    import math

    d_lat = side_meters / 110_574.0
    d_lon = side_meters / (111_320.0 * math.cos(math.radians(REF_LAT)))

    corners = [
        (REF_LON, REF_LAT),
        (REF_LON + d_lon, REF_LAT),
        (REF_LON + d_lon, REF_LAT + d_lat),
        (REF_LON, REF_LAT + d_lat),
        (REF_LON, REF_LAT),
    ]
    coord_text = " ".join(f"{lon},{lat},0" for lon, lat in corners)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Test Parcel</name>
    <Placemark>
      <name>Square Parcel</name>
      <description>A test parcel of {side_meters:.0f}m per side</description>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>{coord_text}</coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
""".encode("utf-8")


def mixed_geometry_kml() -> bytes:
    """Build a KML containing a polygon, a line and a point.

    Exercises the requirement that each geometry type is handled appropriately
    — area for the polygon, length for the line, and a graceful "no measurement"
    for the point — within a single file.

    Returns:
        UTF-8 encoded KML bytes.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Mixed Geometry Sample</name>
    <Placemark>
      <name>Field</name>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>
              {REF_LON},{REF_LAT},0
              {REF_LON + 0.01},{REF_LAT},0
              {REF_LON + 0.01},{REF_LAT + 0.01},0
              {REF_LON},{REF_LAT + 0.01},0
              {REF_LON},{REF_LAT},0
            </coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark>
      <name>Access Road</name>
      <LineString>
        <coordinates>
          {REF_LON},{REF_LAT},0
          {REF_LON + 0.02},{REF_LAT + 0.02},0
        </coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>Survey Marker</name>
      <Point>
        <coordinates>{REF_LON},{REF_LAT},0</coordinates>
      </Point>
    </Placemark>
  </Document>
</kml>
""".encode("utf-8")


def shapefile_zip(
    tmp_path: Path,
    *,
    include_prj: bool = True,
    geometry_kind: str = "polygon",
) -> bytes:
    """Build a zipped shapefile with attributes and a known geometry type.

    The shapefile format permits only **one** geometry type per layer, so
    unlike the KML fixtures this builder produces a homogeneous layer and takes
    ``geometry_kind`` to choose which.

    Args:
        tmp_path: A pytest ``tmp_path`` to stage the shapefile parts in.
        include_prj: When False, the ``.prj`` sidecar is dropped so the CRS
            must be assumed — exercising the undeclared-CRS path.
        geometry_kind: ``"polygon"``, ``"line"`` or ``"point"``.

    Returns:
        The ZIP archive as bytes.
    """
    staging = tmp_path / f"shp_build_{geometry_kind}"
    staging.mkdir(parents=True, exist_ok=True)

    if geometry_kind == "polygon":
        geometries = [
            Polygon(
                [
                    (REF_LON, REF_LAT),
                    (REF_LON + 0.01, REF_LAT),
                    (REF_LON + 0.01, REF_LAT + 0.01),
                    (REF_LON, REF_LAT + 0.01),
                ]
            ),
            Polygon(
                [
                    (REF_LON + 0.05, REF_LAT),
                    (REF_LON + 0.06, REF_LAT),
                    (REF_LON + 0.06, REF_LAT + 0.01),
                    (REF_LON + 0.05, REF_LAT + 0.01),
                ]
            ),
        ]
    elif geometry_kind == "line":
        geometries = [
            LineString([(REF_LON, REF_LAT), (REF_LON + 0.02, REF_LAT + 0.02)]),
            LineString([(REF_LON, REF_LAT), (REF_LON + 0.01, REF_LAT)]),
        ]
    else:
        geometries = [
            Point(REF_LON, REF_LAT),
            Point(REF_LON + 0.01, REF_LAT + 0.01),
        ]

    gdf = gpd.GeoDataFrame(
        {
            "name": ["Parcel A", "Parcel B"],
            "owner": ["Alice", "Bob"],
            "parcel_no": [101, 202],
        },
        geometry=geometries,
        crs="EPSG:4326",
    )

    shp_path = staging / "parcels.shp"
    gdf.to_file(shp_path)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for part in staging.glob("parcels.*"):
            if not include_prj and part.suffix.lower() == ".prj":
                continue
            zf.write(part, arcname=part.name)

    return buffer.getvalue()


def incomplete_shapefile_zip() -> bytes:
    """Build a ZIP with a ``.shp`` but no ``.shx``/``.dbf`` sidecars.

    Returns:
        The ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("parcels.shp", b"\x00\x00\x27\x0a" + b"\x00" * 60)
    return buffer.getvalue()


def zip_without_shapefile() -> bytes:
    """Build a valid ZIP that contains no shapefile at all.

    Returns:
        The ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("readme.txt", "This archive has no geospatial data.")
        zf.writestr("notes.csv", "a,b,c\n1,2,3\n")
    return buffer.getvalue()


def zip_slip_archive() -> bytes:
    """Build a ZIP with a parent-relative entry path (zip-slip attempt).

    Returns:
        The ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../../escaped.shp", b"malicious")
        zf.writestr("parcels.shp", b"\x00" * 10)
        zf.writestr("parcels.shx", b"\x00" * 10)
        zf.writestr("parcels.dbf", b"\x00" * 10)
    return buffer.getvalue()


def zip_bomb_archive() -> bytes:
    """Build a small ZIP that expands far beyond the permitted ratio.

    Returns:
        The ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 80 MB of zeros compresses to a few kilobytes.
        zf.writestr("parcels.shp", b"\x00" * (80 * 1024 * 1024))
        zf.writestr("parcels.shx", b"\x00" * 10)
        zf.writestr("parcels.dbf", b"\x00" * 10)
    return buffer.getvalue()


def malformed_kml() -> bytes:
    """Return KML bytes with unbalanced tags, which no driver can parse."""
    return b"<?xml version='1.0'?><kml><Document><Placemark>"
