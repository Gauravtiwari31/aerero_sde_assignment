"""
Archive Safety & Shapefile Validation
=====================================
Validates uploaded ``.zip`` archives before any geospatial driver touches them.

An uploaded ZIP is untrusted input. Three classes of problem are handled here:

* **Zip-slip** — entries whose names contain ``..`` or absolute paths, which can
  escape the extraction directory and overwrite arbitrary files.
* **Zip bombs** — small archives that expand to an enormous size, exhausting
  disk or memory.
* **Incomplete shapefiles** — a "shapefile" is really a *set* of sidecar files.
  Without ``.shp``, ``.shx`` and ``.dbf`` the dataset cannot be read, and the
  driver's own error for this is opaque.

Catching these here means the failures surface as precise, actionable 4xx
responses instead of driver stack traces or a filled disk.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.exceptions import CorruptArchiveError, InvalidShapefileError

logger = logging.getLogger(__name__)

#: The three sidecar files that together form a readable shapefile.
REQUIRED_SHAPEFILE_PARTS = {".shp", ".shx", ".dbf"}

#: Reject archives whose uncompressed size exceeds this multiple of their
#: compressed size. Legitimate shapefile archives compress well but rarely
#: beyond ~100x; a 1000x ratio is a strong zip-bomb signal.
MAX_COMPRESSION_RATIO = 1000

#: Hard ceiling on total uncompressed bytes, regardless of ratio.
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB


@dataclass(frozen=True)
class ShapefileArchiveInfo:
    """Summary of a validated shapefile archive.

    Attributes:
        shp_entry:         Path of the ``.shp`` entry inside the archive.
        layer_name:        Base name of the shapefile layer.
        has_projection:    Whether a ``.prj`` sidecar is present. When absent,
                           the CRS must be assumed.
        entry_count:       Number of entries in the archive.
        uncompressed_size: Total uncompressed bytes.
    """

    shp_entry: str
    layer_name: str
    has_projection: bool
    entry_count: int
    uncompressed_size: int


def _is_unsafe_entry(name: str) -> bool:
    """Return True when an archive entry name would escape the extraction root."""
    if name.startswith("/") or name.startswith("\\"):
        return True
    # Normalise Windows separators before inspecting path parts.
    parts = Path(name.replace("\\", "/")).parts
    if ".." in parts:
        return True
    # Drive letters (``C:\...``) are absolute on Windows.
    if len(name) > 1 and name[1] == ":":
        return True
    return False


def validate_shapefile_archive(archive_path: Path) -> ShapefileArchiveInfo:
    """Validate that ``archive_path`` is a safe, complete shapefile archive.

    Args:
        archive_path: Path to the uploaded ``.zip`` on disk.

    Returns:
        A :class:`ShapefileArchiveInfo` describing the validated archive.

    Raises:
        CorruptArchiveError: The file is not a readable ZIP, has a corrupt
            entry, or trips a zip-slip / zip-bomb guard.
        InvalidShapefileError: The archive is readable but does not contain a
            complete shapefile set.
    """
    if not zipfile.is_zipfile(archive_path):
        raise CorruptArchiveError(
            "Uploaded file is not a valid ZIP archive.",
            detail="The bytes received could not be opened as a ZIP container.",
        )

    try:
        with zipfile.ZipFile(archive_path) as zf:
            bad_entry = zf.testzip()
            if bad_entry is not None:
                raise CorruptArchiveError(
                    "ZIP archive contains a corrupt entry.",
                    detail=f"First failing entry: {bad_entry}",
                )

            infos = zf.infolist()
            if not infos:
                raise CorruptArchiveError("ZIP archive is empty.")

            # ── Zip-slip guard ───────────────────────────────────────────
            for info in infos:
                if _is_unsafe_entry(info.filename):
                    raise CorruptArchiveError(
                        "ZIP archive contains an unsafe entry path.",
                        detail=(
                            f"Entry {info.filename!r} uses an absolute or "
                            "parent-relative path, which could escape the "
                            "extraction directory."
                        ),
                    )

            # ── Zip-bomb guard ───────────────────────────────────────────
            uncompressed = sum(i.file_size for i in infos)
            compressed = sum(i.compress_size for i in infos) or 1

            if uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise CorruptArchiveError(
                    "ZIP archive expands beyond the permitted size.",
                    detail=(
                        f"Uncompressed size {uncompressed:,} bytes exceeds the "
                        f"{MAX_UNCOMPRESSED_BYTES:,} byte limit."
                    ),
                )

            if uncompressed / compressed > MAX_COMPRESSION_RATIO:
                raise CorruptArchiveError(
                    "ZIP archive has a suspicious compression ratio.",
                    detail=(
                        f"Expands {uncompressed // compressed}x, above the "
                        f"{MAX_COMPRESSION_RATIO}x limit — rejected as a "
                        "potential decompression bomb."
                    ),
                )

            # ── Shapefile completeness ───────────────────────────────────
            names = [i.filename for i in infos if not i.is_dir()]
            shp_entries = [n for n in names if n.lower().endswith(".shp")]

            if not shp_entries:
                found = ", ".join(sorted(names)[:10]) or "no files"
                raise InvalidShapefileError(
                    "ZIP archive does not contain a .shp file.",
                    detail=(
                        "A shapefile upload must include a .shp file alongside "
                        f"its .shx and .dbf sidecars. Found: {found}."
                    ),
                )

            # Deterministically pick the first layer when several are bundled.
            shp_entry = sorted(shp_entries)[0]
            stem = shp_entry[: -len(".shp")]
            layer_name = Path(stem).name

            present = {
                n[len(stem):].lower()
                for n in names
                if n.lower().startswith(stem.lower())
            }
            missing = REQUIRED_SHAPEFILE_PARTS - present - {".shp"}
            if missing:
                raise InvalidShapefileError(
                    "Shapefile set is incomplete.",
                    detail=(
                        f"Layer {layer_name!r} is missing required sidecar "
                        f"file(s): {', '.join(sorted(missing))}."
                    ),
                )

            if len(shp_entries) > 1:
                logger.info(
                    "Archive contains %d layers; processing %r",
                    len(shp_entries),
                    layer_name,
                )

            return ShapefileArchiveInfo(
                shp_entry=shp_entry,
                layer_name=layer_name,
                has_projection=".prj" in present,
                entry_count=len(names),
                uncompressed_size=uncompressed,
            )

    except (CorruptArchiveError, InvalidShapefileError):
        raise
    except zipfile.BadZipFile as exc:
        raise CorruptArchiveError(
            "ZIP archive could not be read.", detail=str(exc)
        ) from exc
