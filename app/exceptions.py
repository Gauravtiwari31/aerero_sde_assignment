"""
Domain Exceptions
=================
Typed exception hierarchy for the geospatial processing pipeline.

Using domain-specific exceptions (rather than raising ``ValueError`` or
``HTTPException`` from deep inside the processing layer) keeps the business
logic framework-agnostic and lets the API layer map each failure mode onto an
accurate HTTP status code and a stable, machine-readable error code.
"""

from __future__ import annotations


class GeospatialError(Exception):
    """Base class for all domain errors raised by the processing pipeline."""

    code: str = "GEOSPATIAL_ERROR"
    http_status: int = 400

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class UnsupportedFileTypeError(GeospatialError):
    """Raised when the uploaded file extension is not accepted."""

    code = "UNSUPPORTED_FILE_TYPE"
    http_status = 415


class FileTooLargeError(GeospatialError):
    """Raised when the upload exceeds the configured size ceiling."""

    code = "FILE_TOO_LARGE"
    http_status = 413


class CorruptArchiveError(GeospatialError):
    """Raised when a ``.zip`` cannot be opened or fails safety validation."""

    code = "CORRUPT_ARCHIVE"
    http_status = 422


class InvalidShapefileError(GeospatialError):
    """Raised when an archive does not contain a usable shapefile set."""

    code = "INVALID_SHAPEFILE"
    http_status = 422


class UnreadableGeospatialFileError(GeospatialError):
    """Raised when the geospatial driver cannot parse the file."""

    code = "UNREADABLE_FILE"
    http_status = 422


class EmptyDatasetError(GeospatialError):
    """Raised when a file parses correctly but contains zero features."""

    code = "EMPTY_DATASET"
    http_status = 422


class FileNotFoundError_(GeospatialError):
    """Raised when a requested file record does not exist."""

    code = "FILE_NOT_FOUND"
    http_status = 404


class FileNotReadyError(GeospatialError):
    """Raised when measurements are requested before processing completes."""

    code = "FILE_NOT_READY"
    http_status = 409


class ProcessingFailedError(GeospatialError):
    """Raised when measurements are requested for a file that failed."""

    code = "PROCESSING_FAILED"
    http_status = 422
