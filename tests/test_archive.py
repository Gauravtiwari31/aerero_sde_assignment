"""
Archive Safety Tests
====================
Verifies that malicious and malformed ZIP archives are rejected before any
geospatial driver sees them, and that valid archives pass through.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.archive import validate_shapefile_archive
from app.exceptions import CorruptArchiveError, InvalidShapefileError
from tests.fixtures import (
    incomplete_shapefile_zip,
    shapefile_zip,
    zip_bomb_archive,
    zip_slip_archive,
    zip_without_shapefile,
)


def _write(tmp_path, name: str, data: bytes):
    """Write bytes to a temp file and return its path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ── Valid archives ───────────────────────────────────────────────────────

def test_valid_shapefile_archive_is_accepted(tmp_path):
    """A complete shapefile bundle validates and reports its layer."""
    path = _write(tmp_path, "good.zip", shapefile_zip(tmp_path))
    info = validate_shapefile_archive(path)

    assert info.layer_name == "parcels"
    assert info.shp_entry.endswith(".shp")
    assert info.has_projection is True
    assert info.uncompressed_size > 0


def test_archive_without_prj_is_accepted_but_flagged(tmp_path):
    """A missing .prj is legal; the CRS simply has to be assumed later."""
    path = _write(tmp_path, "noprj.zip", shapefile_zip(tmp_path, include_prj=False))
    info = validate_shapefile_archive(path)

    assert info.has_projection is False, "The missing projection must be reported"


# ── Security guards ──────────────────────────────────────────────────────

def test_zip_slip_entry_is_rejected(tmp_path):
    """An entry path containing '..' is refused as a traversal attempt."""
    path = _write(tmp_path, "slip.zip", zip_slip_archive())

    with pytest.raises(CorruptArchiveError) as exc:
        validate_shapefile_archive(path)

    assert "unsafe entry path" in exc.value.message.lower()
    assert "escaped.shp" in (exc.value.detail or "")


def test_absolute_entry_path_is_rejected(tmp_path):
    """An absolute entry path is refused."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("/etc/passwd", b"root")
    path = _write(tmp_path, "abs.zip", buffer.getvalue())

    with pytest.raises(CorruptArchiveError):
        validate_shapefile_archive(path)


@pytest.mark.slow
def test_zip_bomb_is_rejected(tmp_path):
    """An archive with an extreme compression ratio is refused."""
    path = _write(tmp_path, "bomb.zip", zip_bomb_archive())

    with pytest.raises(CorruptArchiveError) as exc:
        validate_shapefile_archive(path)

    assert "compression ratio" in exc.value.message.lower()


# ── Malformed input ──────────────────────────────────────────────────────

def test_non_zip_bytes_are_rejected(tmp_path):
    """Arbitrary bytes named .zip are refused with a clear message."""
    path = _write(tmp_path, "fake.zip", b"this is definitely not a zip file")

    with pytest.raises(CorruptArchiveError) as exc:
        validate_shapefile_archive(path)

    assert "not a valid zip" in exc.value.message.lower()


def test_empty_zip_is_rejected(tmp_path):
    """A ZIP with no entries is refused."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    path = _write(tmp_path, "empty.zip", buffer.getvalue())

    with pytest.raises(CorruptArchiveError) as exc:
        validate_shapefile_archive(path)

    assert "empty" in exc.value.message.lower()


def test_zip_without_shp_is_rejected(tmp_path):
    """A valid ZIP carrying no .shp is refused with an actionable message."""
    path = _write(tmp_path, "nodata.zip", zip_without_shapefile())

    with pytest.raises(InvalidShapefileError) as exc:
        validate_shapefile_archive(path)

    assert ".shp" in exc.value.message
    assert "readme.txt" in (exc.value.detail or "")


def test_incomplete_shapefile_set_is_rejected(tmp_path):
    """A .shp without its .shx/.dbf sidecars is refused, naming what is missing."""
    path = _write(tmp_path, "partial.zip", incomplete_shapefile_zip())

    with pytest.raises(InvalidShapefileError) as exc:
        validate_shapefile_archive(path)

    detail = exc.value.detail or ""
    assert ".shx" in detail and ".dbf" in detail, (
        "The error must name the specific missing sidecars"
    )


def test_error_codes_are_stable(tmp_path):
    """Domain errors expose machine-readable codes for API consumers."""
    path = _write(tmp_path, "nodata.zip", zip_without_shapefile())

    with pytest.raises(InvalidShapefileError) as exc:
        validate_shapefile_archive(path)

    assert exc.value.code == "INVALID_SHAPEFILE"
    assert exc.value.http_status == 422
