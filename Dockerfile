# ── Build stage ───────────────────────────────────────────────────────────
# GDAL/GEOS/PROJ are native libraries that the Python geospatial stack binds
# to. The wheels on PyPI bundle them, so a slim base image is sufficient and
# avoids the much larger osgeo/gdal image.
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────
FROM python:3.11-slim

# Run as a non-root user: the service writes uploaded files to disk, so a
# container escape should not land on a root-owned filesystem.
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app/ ./app/

RUN mkdir -p /app/data/uploads && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UPLOAD_DIR=/app/data/uploads \
    DATABASE_URL=sqlite+aiosqlite:////app/data/geospatial.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health/',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
