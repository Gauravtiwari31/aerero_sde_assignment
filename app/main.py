"""
Application Entrypoint
======================
Builds the FastAPI application: lifecycle, middleware, exception handling and
routing.

Error handling
--------------
Every failure leaves through one of the handlers registered here, so clients
always receive the same JSON envelope — ``{error, message, detail, status_code}``
— whether the cause was a validation failure, a domain error, or an unexpected
exception. Internal exception text is logged but never leaked to the caller.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import router
from app.config import get_settings
from app.database import init_db
from app.exceptions import GeospatialError

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and the upload directory on startup."""
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    await init_db()
    logger.info("Ready — docs at /docs")
    yield
    logger.info("Shutting down")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    description=(
        "Upload a Shapefile (`.zip`) or KML (`.kml`), and the service extracts "
        "every feature, reprojects it to an appropriate metre-based CRS, and "
        "returns area and length measurements.\n\n"
        "**Measurements are never computed on geographic coordinates.** Each "
        "dataset is reprojected to the UTM zone covering its centroid, and the "
        "chosen CRS plus the reasoning behind it is returned alongside the "
        "numbers so every result is auditable."
    ),
    openapi_tags=[
        {"name": "Files", "description": "Upload, inspect and measure geospatial files."},
        {"name": "Operations", "description": "Health and service statistics."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a correlation ID and timing to every request.

    The ID is echoed in ``X-Request-ID`` and included in error responses, which
    lets a user quote it when reporting a problem and makes the matching log
    line findable immediately.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        logger.exception(
            "[%s] %s %s failed after %.1fms",
            request_id,
            request.method,
            request.url.path,
            elapsed,
        )
        raise

    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{elapsed:.1f}"
    logger.info(
        "[%s] %s %s -> %d (%.1fms)",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )
    return response


def _error(
    request: Request,
    *,
    code: str,
    message: str,
    detail: str | None,
    status_code: int,
) -> JSONResponse:
    """Render the standard error envelope."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": code,
            "message": message,
            "detail": detail,
            "status_code": status_code,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(GeospatialError)
async def handle_geospatial_error(request: Request, exc: GeospatialError) -> JSONResponse:
    """Map a domain error onto its designated status code."""
    return _error(
        request,
        code=exc.code,
        message=exc.message,
        detail=exc.detail,
        status_code=exc.http_status,
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalise FastAPI's ``HTTPException`` into the shared envelope."""
    codes = {
        400: "BAD_REQUEST",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        413: "FILE_TOO_LARGE",
        415: "UNSUPPORTED_FILE_TYPE",
        422: "UNPROCESSABLE_ENTITY",
    }
    return _error(
        request,
        code=codes.get(exc.status_code, "HTTP_ERROR"),
        message=str(exc.detail),
        detail=None,
        status_code=exc.status_code,
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Summarise request-validation failures into a readable message."""
    problems = [
        f"{'.'.join(str(p) for p in err['loc'][1:]) or 'body'}: {err['msg']}"
        for err in exc.errors()
    ]
    return _error(
        request,
        code="VALIDATION_ERROR",
        message="The request could not be validated.",
        detail="; ".join(problems),
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all that logs the cause but never leaks internals to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error(
        request,
        code="INTERNAL_ERROR",
        message="An unexpected internal error occurred.",
        detail=None,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


app.include_router(router, prefix="/api", tags=["Files"])


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send browsers straight to the interactive documentation."""
    return RedirectResponse(url="/docs")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
