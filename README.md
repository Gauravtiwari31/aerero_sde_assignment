# Geospatial File Measurement API

A FastAPI service that accepts a Shapefile (`.zip`) or KML (`.kml`), extracts every
feature, reprojects it to an appropriate metre-based coordinate system, and returns
area and length measurements.

[![CI](https://github.com/Gauravtiwari31/aerero_sde_assignment/actions/workflows/ci.yml/badge.svg)](https://github.com/Gauravtiwari31/aerero_sde_assignment/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![Tests](https://img.shields.io/badge/tests-70%20passing-brightgreen)
![Coverage](https://img.shields.io/badge/coverage-84%25-green)

> **The central guarantee:** measurements are never computed on latitude/longitude
> degrees. Every dataset is reprojected to the UTM zone covering its centroid before
> a single number is calculated, and the API returns the CRS it used along with the
> reasoning, so every measurement is auditable.

---

## Table of Contents

- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [CRS Handling](#crs-handling)
- [Design Decisions](#design-decisions)
- [Testing](#testing)
- [Learnings](#learnings)
- [Future Scope](#future-scope)

---

## Quick Start

### Prerequisites

- Python 3.10 or newer
- No system GDAL installation needed — the geospatial wheels bundle their own
  native libraries.

### Local setup

```bash
git clone https://github.com/Gauravtiwari31/aerero_sde_assignment.git
cd aerero_sde_assignment

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env            # optional; defaults work out of the box

uvicorn app.main:app --reload
```

The API is then available at `http://127.0.0.1:8000`, with interactive
documentation at **http://127.0.0.1:8000/docs**.

### Docker

```bash
docker compose up --build
```

### Try it

```bash
# Upload a KML
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@survey.kml"

# Check status (processing is asynchronous)
curl http://127.0.0.1:8000/api/files/{id}/

# Fetch measurements
curl http://127.0.0.1:8000/api/files/{id}/measurements/
```

---

## API Reference

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload a `.zip` Shapefile or `.kml` |
| `GET` | `/api/files/` | List uploads (paginated, filterable by status) |
| `GET` | `/api/files/{id}/` | File status, CRS details and result summary |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements (paginated) |
| `GET` | `/api/files/{id}/geojson/` | Export as a GeoJSON FeatureCollection |
| `DELETE` | `/api/files/{id}/` | Delete a file and its measurements |
| `GET` | `/api/health/` | Liveness and dependency check |
| `GET` | `/api/stats/` | Service-wide statistics |

### Upload a file

```http
POST /api/files/
Content-Type: multipart/form-data
```

```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@survey.kml"
```

**`202 Accepted`**

```json
{
  "id": "7af16264-0eeb-4693-b690-d04f97c1e275",
  "filename": "survey.kml",
  "file_type": "kml",
  "file_size_bytes": 913,
  "status": "PENDING",
  "message": "File accepted and queued for processing.",
  "links": {
    "self": "/api/files/7af16264-0eeb-4693-b690-d04f97c1e275/",
    "measurements": "/api/files/7af16264-0eeb-4693-b690-d04f97c1e275/measurements/",
    "geojson": "/api/files/7af16264-0eeb-4693-b690-d04f97c1e275/geojson/"
  }
}
```

Processing happens in the background, so the response returns immediately. Poll
`GET /api/files/{id}/` until `status` is `COMPLETED` or `FAILED`.

### File information

```http
GET /api/files/{id}/
```

**`200 OK`**

```json
{
  "id": "7af16264-0eeb-4693-b690-d04f97c1e275",
  "filename": "survey.kml",
  "file_type": "kml",
  "file_size_bytes": 913,
  "feature_count": 3,
  "crs": "EPSG:4326",
  "crs_details": {
    "source": "EPSG:4326",
    "projected": "EPSG:32643",
    "strategy": "utm_centroid",
    "rationale": "Source CRS EPSG:4326 uses angular units, so measurement would be invalid. Reprojected to EPSG:32643, the UTM zone covering the dataset's centroid, which preserves distance and area to sub-metre accuracy across the zone.",
    "assumed": false
  },
  "status": "COMPLETED",
  "error_code": null,
  "error_message": null,
  "processing_ms": 241,
  "uploaded_at": "2026-10-07T07:05:00.658812",
  "processed_at": "2026-10-07T07:05:00.921745"
}
```

### Measurements

```http
GET /api/files/{id}/measurements/
```

**Query parameters**

| Parameter | Default | Description |
|---|---|---|
| `skip` | `0` | Features to skip |
| `limit` | `100` | Page size (max 1000) |
| `geometry_type` | — | Filter to one type, e.g. `Polygon` |
| `measurable_only` | `false` | Return only features with a measurement |
| `include_geometry` | `true` | Set `false` to omit WKT/GeoJSON and shrink the payload |

**`200 OK`** (abridged)

```json
{
  "file_id": "7af16264-0eeb-4693-b690-d04f97c1e275",
  "filename": "survey.kml",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "summary": {
    "total_features": 3,
    "measured_features": 2,
    "unsupported_features": 0,
    "invalid_geometries": 0,
    "geometry_type_counts": { "Polygon": 1, "LineString": 1, "Point": 1 },
    "total_area_sq_meters": 1201684.72,
    "total_area_hectares": 120.17,
    "total_length_meters": 3100.84,
    "total_length_kilometers": 3.10
  },
  "measurements": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "original_crs": "EPSG:4326",
      "projected_crs": "EPSG:32643",
      "is_measurable": true,
      "area_sq_meters": 1201684.7167952391,
      "area_sq_kilometers": 1.2016847167952391,
      "area_hectares": 120.16847167952392,
      "area_acres": 296.9427603469501,
      "perimeter_meters": 4385.063458845189,
      "length_meters": null,
      "vertex_count": 5,
      "part_count": 1,
      "is_valid": true,
      "properties": { "Name": "Field" },
      "measurement_note": null
    },
    {
      "feature_index": 2,
      "geometry_type": "Point",
      "is_measurable": false,
      "area_sq_meters": null,
      "length_meters": null,
      "properties": { "Name": "Survey Marker" },
      "measurement_note": "No measurement is defined for Point geometries."
    }
  ]
}
```

Per the specification, **Polygons return area, LineStrings return length, and
Points return neither** — the Point is reported with an explanatory note rather
than being treated as an error.

### GeoJSON export

```http
GET /api/files/{id}/geojson/
```

Returns `application/geo+json` with each feature's measurements merged into its
properties under `_measurements`. Loads directly into QGIS, Leaflet, Mapbox or
geojson.io.

### Error format

Every non-2xx response uses one envelope:

```json
{
  "error": "INVALID_SHAPEFILE",
  "message": "Shapefile set is incomplete.",
  "detail": "Layer 'parcels' is missing required sidecar file(s): .dbf.",
  "status_code": 422,
  "request_id": "99416601-c621-4011-9ce2-f232870bb861"
}
```

| Code | Status | Meaning |
|---|---|---|
| `UNSUPPORTED_FILE_TYPE` | 415 | Extension is not `.zip` or `.kml` |
| `FILE_TOO_LARGE` | 413 | Upload exceeds the configured cap |
| `CORRUPT_ARCHIVE` | 422 | ZIP is unreadable, unsafe, or a decompression bomb |
| `INVALID_SHAPEFILE` | 422 | Archive lacks a complete `.shp`/`.shx`/`.dbf` set |
| `UNREADABLE_FILE` | 422 | The driver could not parse the file |
| `EMPTY_DATASET` | 422 | File parsed but holds no features |
| `NOT_FOUND` | 404 | No such file id |
| `CONFLICT` | 409 | Measurements requested while still processing |
| `VALIDATION_ERROR` | 422 | Invalid query or body parameters |

Every response also carries `X-Request-ID` and `X-Process-Time-Ms` headers. A
client-supplied `X-Request-ID` is echoed back, so a request can be traced from
the caller's logs into the server's.

---

## Architecture

### Application structure

```
app/
├── main.py          FastAPI app: lifecycle, middleware, error handlers
├── api.py           HTTP endpoints and request/response mapping
├── schemas.py       Pydantic contracts (the public API shape)
├── models.py        SQLAlchemy ORM models
├── database.py      Async engine, session factory, table creation
├── config.py        Environment-driven settings
├── geospatial.py    Processing pipeline orchestration
├── crs.py           CRS resolution strategy          <- requirement 5
├── measurement.py   Pure geometry measurement        <- requirement 4
├── archive.py       ZIP safety and shapefile validation
└── exceptions.py    Typed domain errors

tests/
├── conftest.py        Isolated DB and upload dir per run
├── fixtures.py        Real KML/Shapefile builders with known ground truth
├── test_measurement.py  21 unit tests
├── test_crs.py           9 projection-correctness tests
├── test_archive.py      10 security tests
└── test_api.py          30 end-to-end tests
```

The layering is deliberate: `measurement.py` and `crs.py` know nothing about
HTTP or databases, so the numeric behaviour that matters most can be tested
directly, without a server or a file on disk.

### File-processing flow

```
POST /api/files/
   │
   ├─ validate extension (.zip / .kml)
   ├─ stream to disk in 1 MiB chunks, enforcing the size cap mid-stream
   ├─ compute SHA-256 while streaming
   ├─ persist record with status=PENDING
   └─ return 202 immediately
          │
          └─ background thread (asyncio.to_thread)
                 │
                 ├─ 1. VALIDATE   ZIP safety + shapefile completeness
                 ├─ 2. READ       GeoPandas/Fiona -> GeoDataFrame
                 ├─ 3. PROJECT    resolve a metre-based CRS, reproject
                 ├─ 4. MEASURE    per-feature area/length
                 └─ 5. PERSIST    bulk-insert features + dataset summary
                        │
                        └─ status=COMPLETED, or FAILED with a typed code
```

Processing is asynchronous because a large shapefile can take many seconds, and
holding an HTTP connection open for it is a poor contract. GDAL work is
CPU-bound and releases no event loop time, so it runs in a worker thread rather
than inline on the loop.

Failure is contained at two levels: a single odd *feature* degrades to "no
measurement" with a note, while a failed *stage* marks the file `FAILED` with a
machine-readable code. One unusual geometry never fails an otherwise valid upload.

### Measurement calculation flow

For each feature, the source frame supplies the geometry and attributes while the
projected frame supplies the numbers:

| Geometry | Measurement |
|---|---|
| Polygon / MultiPolygon | area + perimeter |
| LineString / MultiLineString / LinearRing | length |
| Point / MultiPoint | none (reported, not an error) |
| GeometryCollection | recursive, summed per part |
| Anything else | note explaining it is unsupported |

Results are returned in several units (m², km², hectares, acres; m, km, miles)
because clients routinely want hectares for parcels and kilometres for routes.
Deriving them once server-side avoids every consumer re-implementing the
conversion and drifting on rounding.

Each feature also carries `vertex_count`, `part_count` and `is_valid`. Validity
matters: a self-intersecting polygon still has a computable area, but that area
is unreliable — so the value is returned *with* a flag rather than silently
presented as trustworthy.

---

## CRS Handling

This is requirement 5, and the part most worth getting right.

### Why it matters

A degree is not a unit of distance. Computing `polygon.area` on EPSG:4326
coordinates produces square degrees — a number whose ground meaning varies with
latitude and which is off by roughly **ten orders of magnitude**. A test in
`tests/test_crs.py` asserts exactly this gap, so the failure mode is documented
rather than merely avoided.

### The strategy

Resolution happens in `app/crs.py`, in priority order:

1. **Source already projected and metre-based** → measure in place. Reprojecting
   would only add resampling error.
2. **Geographic input** → reproject to the **UTM zone containing the dataset's
   centroid**, via `estimate_utm_crs()`.
3. **UTM estimation fails** → fall back to the configured
   `DEFAULT_PROJECTED_CRS`, and say so in the rationale.

A dataset with no declared CRS is assumed to be WGS84 — the near-universal
default, and mandatory for KML — and the response flags `assumed: true` so the
caller knows it was an inference rather than a reading.

### Why UTM rather than Web Mercator

Web Mercator (EPSG:3857) is ubiquitous for map tiles and tempting as a single
global projection, but its area distortion grows with the secant of latitude:
roughly **2x at 45°**, and far worse approaching the poles. It is unsuitable for
measurement. UTM gives sub-metre accuracy across its zone, which is the right
trade for survey-scale work.

The known limitation is that a dataset spanning multiple UTM zones is measured
in the single zone covering its centroid, so features far from that centroid
accumulate distortion. For the parcel-and-route data this service targets, that
is the correct trade; see [Future Scope](#future-scope) for the fix.

### Auditability

The chosen CRS is never implicit. Every response carries `crs_details` with the
source, the projected CRS, the strategy that fired, and a plain-language
rationale — so a reviewer can check not just the number but the projection it
came from and why.

---

## Design Decisions

### FastAPI over Django + DRF

The service is a small, stateless API with no need for an admin, templating, or
Django's ORM-coupled app structure. FastAPI gives native async (useful for
non-blocking uploads), Pydantic validation that doubles as the API contract, and
OpenAPI documentation generated from the code rather than maintained alongside it.

### Background processing over synchronous parsing

**Chosen:** return `202 Accepted` immediately, process in a worker thread.

A large shapefile can take many seconds to parse. Holding the connection open
invites client timeouts and makes the service trivially easy to exhaust.

**Alternative considered:** Celery + Redis. That is the right answer at scale
and is the documented upgrade path, but it adds a broker and a worker process to
a take-home that must run with one `uvicorn` command. The pipeline is already
written as a self-contained function taking primitives, so moving it onto a
Celery task is a near-mechanical change.

### Pre-computed summaries over on-demand aggregation

Aggregate counts and totals live on the file row, denormalised. They could be
derived by scanning `feature_measurements`, but the list and summary endpoints
are read far more often than files are uploaded; recomputing per request would
turn every list call into a full table scan. Writing them once at processing
time is the better trade.

### Enforcing the size limit mid-stream

The upload is written in 1 MiB chunks with the cap checked **during** the write.
Checking afterwards — the more obvious implementation — means an oversized
upload is fully materialised on disk before being rejected, which is precisely
the resource exhaustion the limit exists to prevent.

### Validating archives before the driver sees them

An uploaded ZIP is untrusted input. `app/archive.py` rejects zip-slip paths,
decompression bombs and incomplete shapefile sets *before* GDAL is invoked. This
is both a security boundary and a usability one: "Layer 'parcels' is missing
required sidecar file(s): .dbf" is far more actionable than a GDAL stack trace.

Reading happens through GDAL's `zip://` virtual filesystem, so nothing is ever
extracted to a temporary directory — removing extraction-time attack surface
entirely.

### Typed domain exceptions over raising HTTPException

The processing layer raises `GeospatialError` subclasses that carry a stable
code and an HTTP status. The API layer maps them. This keeps the business logic
framework-agnostic and testable without a request context, while still producing
accurate status codes.

### Pairing frames positionally, not by zipping iterators

Source and projected frames are paired with `.iloc[i]`. The original
implementation zipped two `iterrows()` generators, which silently mispairs
geometries with attributes if either frame is reindexed. A test asserts that
reprojection preserves row order.

### SQLite by default

Zero-setup for a reviewer, and the async SQLAlchemy layer means the swap to
PostgreSQL + PostGIS is a connection-string change. PostGIS would be the choice
in production, since it can perform measurement in the database via
`ST_Area(geography)` and index geometries spatially.

---

## Testing

```bash
pytest                                    # all 70 tests
pytest --cov=app --cov-report=term-missing
pytest tests/test_crs.py -v               # projection correctness
pytest -m "not slow"                      # skip the zip-bomb fixture
```

```
70 passed in 6.84s
Coverage: 84%
```

| Suite | Tests | Focus |
|---|---|---|
| `test_measurement.py` | 21 | Area/length against analytically known values |
| `test_crs.py` | 9 | Reprojection correctness and UTM zone selection |
| `test_archive.py` | 10 | Zip-slip, zip bombs, malformed and incomplete archives |
| `test_api.py` | 30 | End-to-end HTTP, including real GDAL parsing |

**Tests assert ground truth, not current behaviour.** Fixtures are built from
coordinates with independently known dimensions: a KML parcel constructed to be
1 km per side must measure 1,000,000 m² (within 1%), and one degree of latitude
must measure ~110.6 km. Pinning whatever the code happens to return would pass
just as easily against a broken implementation.

The suite also runs against **real** KML and Shapefile data through the actual
GDAL read path — the geospatial layer is not mocked, so driver-level regressions
surface here rather than in production.

---

## Learnings

**Degrees are not metres, and the failure is silent.** `polygon.area` on
EPSG:4326 returns a number — no error, no warning, just a value that is wrong by
ten orders of magnitude. The most consequential bug in a geospatial service is
one that produces plausible-looking output. That is why the CRS decision is
returned with every response rather than kept internal: a number you cannot
audit is a number you cannot trust.

**A shapefile is a set of files, not a file.** Without `.shx` and `.dbf` the
dataset is unreadable, and without `.prj` the CRS must be inferred. Discovering
this through GDAL's error messages is unpleasant, which is the whole argument
for validating the archive and naming precisely what is missing.

**The shapefile format allows only one geometry type per layer.** I learned this
the direct way: my first test fixture mixed polygons, lines and points into one
shapefile and the writer rejected it. The fixture was wrong, not the library —
and it is exactly why KML (which has no such restriction) is the better vehicle
for the mixed-geometry test.

**Graceful degradation needs two levels.** Treating every problem as fatal means
one odd geometry fails a 10,000-feature upload. Treating nothing as fatal means
silent data loss. The split here — a bad *feature* gets a note, a bad *stage*
fails the file — came from thinking about which failures a caller can act on.

**CPU-bound work in an async framework needs a thread.** `async def` does not
make GDAL non-blocking; parsing on the event loop stalls every other request.
`asyncio.to_thread` is the fix, and it only matters under concurrency — which is
precisely when it matters most.

---

## Future Scope

**Correctness**

- **Geodesic measurement** on the WGS84 ellipsoid (`pyproj.Geod`) as an opt-in
  mode, removing projection distortion entirely for datasets that span zones.
- **Per-feature UTM selection** for continent-scale datasets, instead of one
  zone chosen from the whole dataset's centroid.
- **Geometry repair** via `make_valid()`, offered as a flag so self-intersecting
  polygons can be measured reliably rather than only flagged.

**Scale**

- **Celery + Redis** for distributed processing, retries and progress reporting
  — the pipeline is already structured as a standalone function to make this
  straightforward.
- **PostgreSQL + PostGIS**, enabling in-database measurement via
  `ST_Area(geography)` and spatial indexing for bounding-box queries.
- **Streaming reads** for very large files, processing in chunks rather than
  loading the full GeoDataFrame into memory.
- **Object storage** (S3/GCS) for uploads, so the service becomes stateless and
  horizontally scalable.

**Features**

- **More formats**: GeoJSON, GeoPackage, GPX, File Geodatabase — the reader is
  already isolated behind one function.
- **Coordinate-system override**, letting a caller declare the CRS for files
  that lack or misdeclare one.
- **WebSocket or webhook** progress notifications, replacing status polling.
- **Spatial queries**: filter features by bounding box or by intersection with a
  supplied geometry.

**Operations**

- **Authentication and per-client rate limiting** — the service is currently
  open, which is appropriate for an assignment and not for deployment.
- **Prometheus metrics and structured JSON logs**, building on the request-ID
  correlation already in place.
- **Content-based deduplication** using the SHA-256 already computed at upload,
  returning the existing result instead of reprocessing identical files.

---

## License

MIT
