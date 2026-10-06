# Geospatial File Measurement API

A FastAPI REST service that accepts KML or a zipped Shapefile, preserves feature
attributes, and calculates polygon areas and line lengths in a local projected
CRS. SQLite keeps processed results available after a restart.

Repository: [SuryaSriramD/Geospatial-File-Measurement](https://github.com/SuryaSriramD/Geospatial-File-Measurement).

## Run locally

Use **Python 3.12+**. The pinned dependencies and tests were verified with Python
3.12. No separate database server or GDAL installation is required.

Clone the repository, then set up the environment (skip the first two commands
if you already have this project open):

```bash
git clone https://github.com/SuryaSriramD/Geospatial-File-Measurement.git
cd Geospatial-File-Measurement
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

On Windows, activate with `.venv\Scripts\activate`. Omit `--reload` outside
development. Browse [Swagger UI](http://127.0.0.1:8000/docs),
[ReDoc](http://127.0.0.1:8000/redoc), or
[OpenAPI JSON](http://127.0.0.1:8000/openapi.json).

## REST API

| Method | Endpoint | Successful response |
|---|---|---|
| `POST` | `/api/files/` | `201 Created`: processed file metadata; `Location` header |
| `GET` | `/api/files/{id}/` | `200 OK`: stored metadata |
| `GET` | `/api/files/{id}/measurements/` | `200 OK`: paginated features and measurements |
| `GET` | `/health` | `200 OK`: process health |

`/health` is a liveness check; it does not verify database availability.

### Upload

Submit `multipart/form-data` with a field named `file`:

```bash
curl -i -X POST http://127.0.0.1:8000/api/files/ \
  -F 'file=@examples/sample.kml'

# Or upload a zipped Shapefile:
curl -i -X POST http://127.0.0.1:8000/api/files/ \
  -F 'file=@survey.zip'
```

The example KML contains a point, a line, and a polygon. A response has this shape
(IDs and timestamps are generated for each upload):

```json
{
  "id": "bc1a2061-2c96-4db0-9d8e-93f619a4bd61",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "geometry_crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-06T12:00:00Z",
  "measurement_summary": {
    "calculated": 2,
    "not_applicable": 1,
    "unsupported": 0,
    "invalid": 0
  }
}
```

Copy the returned `id`, then request the stored resources:

```bash
FILE_ID='paste-the-returned-id-here'
curl "http://127.0.0.1:8000/api/files/$FILE_ID/"
curl "http://127.0.0.1:8000/api/files/$FILE_ID/measurements/?limit=100&offset=0"
```

`limit` defaults to 100 and permits 1–1000. `offset` defaults to 0 and must be
nonnegative. Results are ordered by the zero-based feature `index`. The response
envelope contains `file_id`, `total`, `limit`, `offset`, and `items`.

Each item contains:

```json
{
  "feature_id": "site-1",
  "index": 0,
  "geometry_type": "Point",
  "geometry": {"type": "Point", "coordinates": [78.4867, 17.385]},
  "crs": "EPSG:4326",
  "properties": {"name": "Survey point", "category": "reference"},
  "measurement": {
    "status": "NOT_APPLICABLE",
    "area_m2": null,
    "length_m": null,
    "crs": null,
    "reason": "Point geometries have no area or line length."
  }
}
```

Polygons return `area_m2`; lines return `length_m`. A calculated measurement
includes its projected CRS, such as `EPSG:32644` for the sample. Polygon perimeter
is not returned. Full responses from the included sample are saved in
`examples/sample-upload-response.json` and `examples/sample-measurements-response.json`.
Their IDs illustrate a previous test run; upload the sample to obtain a usable ID.

### File and feature semantics

- A ZIP must contain exactly one dataset with matching `.shp`, `.shx`, `.dbf`, and
  `.prj` components. A shared subdirectory is allowed. Names are matched without
  case sensitivity. `.cpg` controls attribute encoding; the default is UTF-8.
- KML uses WGS84 longitude/latitude. Placemark IDs are preserved when present;
  otherwise IDs use the feature index. Name, description, and ExtendedData are
  preserved. Styling, overlays, network links, and external resources are not
  processed or fetched. KMZ is not accepted.
- Deleted DBF rows are omitted; attributes remain paired with their original
  shape record. The Shapefile feature ID retains that original record index.
- File metadata `crs` describes the **source** CRS. Every returned geometry is
  normalized to WGS84 XY longitude/latitude, identified by `geometry_crs` in file
  metadata and `crs` on each feature. `measurement.crs` is the projected CRS used
  for the calculation. A source without an EPSG identifier is represented by its
  CRS definition.
- Z/altitude and M ordinates do not contribute to horizontal measurements.
- Polygon holes are subtracted. MultiPolygon areas and MultiLineString lengths
  are summed. Homogeneous KML MultiGeometry becomes the corresponding multipart
  geometry. Mixed collections and unsupported KML geometry receive a reason.
  If a KML collection contains unsupported components, its known geometry may be
  returned with an explicit explanation, but no partial measurement is reported.
- `CALCULATED`, `NOT_APPLICABLE`, `UNSUPPORTED`, and `INVALID` describe individual
  features. Unavailable measurements are `null`, never a misleading zero.
  Geometry may also be `null` when absent, invalid, or unsupported by the parser.
- `COMPLETED` means all features were processed; it does not mean every feature
  could be measured. Inspect `measurement_summary` and per-feature status.
- Malformed file structure rejects the entire upload. A geometrically invalid
  feature in an otherwise readable file is reported individually. Features are
  not silently repaired. Original upload bytes are discarded after processing;
  normalized feature data and metadata are persisted.

### Error responses

| HTTP status | Meaning |
|---|---|
| `404` | No uploaded file with this UUID |
| `413` | File or entire request exceeds the size limit |
| `415` | Filename extension is neither `.zip` nor `.kml` |
| `422` | Invalid file, missing CRS/components, unsafe archive/XML, or invalid parameters |
| `503` | SQLite is unavailable |

Application errors use `{"detail": {"code": "...", "message": "..."}}`.
FastAPI's request validation errors use its standard `detail` list. Failed uploads
create no file record. Uploading identical bytes again creates a new resource.

## Architecture

```text
app/
  main.py          REST routes and processing orchestration
  config.py        Environment-backed settings
  middleware.py    Request byte limit before multipart parsing
  parsers.py       Safe ZIP/Shapefile and KML readers
  measurements.py  Geometry validation and CRS-aware measurements
  repository.py    Atomic SQLite persistence and pagination
  schemas.py       Typed response models and OpenAPI contracts
tests/             Parser, numerical, and REST integration checks
examples/          Sample KML and generated response examples
```

Processing flow:

1. Bound the HTTP body, validate the extension and individual file size.
2. Parse features, attributes, and source CRS with feature/coordinate limits.
3. Validate each geometry, convert it to WGS84 XY, and select a metric projection.
4. Calculate the supported measurement or record a per-feature reason.
5. Atomically save file metadata and all features to SQLite, then return `201`.
6. Serve later metadata and paginated feature reads from SQLite.

Parsing and projection run in synchronous FastAPI endpoints, which execute in a
worker thread pool. The upload remains synchronous: it returns after processing
and persistence, without an asynchronous job lifecycle. SQLite uses WAL and a
separate connection per operation. Database writes are parameterized and atomic.

### CRS and measurement strategy

Shapely's area and length operations are planar. The service never treats degrees
as metres or square metres. It transforms source coordinates to WGS84 with
`always_xy=True`, then to a suitable metric CRS before measuring.

For each local feature, its WGS84 bounding box selects a UTM zone and hemisphere.
UTM is used between 80°S and 84°N. Entirely polar features use north/south UPS.
Measurements are intentionally bounded to features spanning at most **6° of
longitude and 6° of latitude**, and UTM features must remain within 6° of the
chosen zone's central meridian. Small shapes across UTM zone boundaries are
allowed. Large or antimeridian-crossing features and features crossing a
UTM/polar transition return `UNSUPPORTED` with an explanation.

These are projected planar measurements with projection distortion, not a
guarantee of survey-grade or geodesic accuracy. Long segments are projected using
their supplied vertices without geodesic densification. A single dataset may use
different measurement CRSs for different features; units remain consistent.

Relevant upstream contracts: [FastAPI file uploads](https://fastapi.tiangolo.com/tutorial/request-files/),
[pyproj coordinate transformation](https://pyproj4.github.io/pyproj/stable/api/transformer.html),
and [Shapely geometry operations](https://shapely.readthedocs.io/en/stable/manual.html).

## Configuration and limits

| Environment variable | Default | Purpose |
|---|---|---|
| `GEO_DATA_DIR` | `data` | Directory containing `geospatial.sqlite3` |
| `GEO_MAX_UPLOAD_BYTES` | `10485760` | Maximum individual upload: 10 MiB |
| `GEO_MAX_FEATURES` | `10000` | Maximum features per upload |

The HTTP request limit is the file limit plus 64 KiB for multipart overhead;
chunked requests are counted too. Request buffering is bounded by that limit.
The parser also enforces 50 MiB of expanded content, 64 ZIP entries, 250,000
coordinates, and limits on XML depth and elements. Raising the upload setting
does not remove parser limits. ZIP files are read in memory without extraction;
unsafe paths, symlinks, encryption, and ambiguous paths are rejected. XML DTDs,
entities, and external references are disabled.

This is a local runnable service. A shared deployment needs authentication,
authorization, rate/concurrency limits, a request timeout and body limit at the
proxy, storage quotas/retention, and appropriate TLS configuration. A size limit
on each request does not bound aggregate memory across concurrent requests.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Tests generate their own Shapefiles and KML. They cover known metric geometry,
geographic and projected input, polygon holes, multipart features, axis order,
polar and southern projections, invalid/unsupported geometry, malicious inputs,
HTTP errors, chunked body limits, pagination, and persistence after restart.

Verified locally: **87 tests passed**, Ruff lint/format checks passed, dependency
compatibility passed, and real Uvicorn HTTP checks passed for upload, metadata,
measurements, health, Swagger UI, and OpenAPI.

The installed Starlette version emits a deprecation warning when its TestClient
uses HTTPX. It does not affect the passing tests or production endpoints.

## Design decisions and alternatives

- **FastAPI and REST:** typed contracts, multipart uploads, clear resource URLs,
  status codes, and generated interactive API documentation.
- **PyShp + defusedxml:** a small dependency set for two explicit formats. A GDAL
  stack such as Fiona/GeoPandas would broaden format support and KML coverage, at
  the cost of additional native dependencies and driver behavior to manage.
- **Shapely + pyproj:** separate geometry operations from coordinate-system
  transformations; avoid degree-based measurements and Web Mercator as a default
  measurement projection.
- **SQLite:** simple persistent local setup. PostgreSQL/PostGIS and object storage
  are better choices for spatial queries, larger datasets, and several servers.
- **Synchronous bounded uploads:** straightforward failure handling and atomic
  completion. A durable queue with separate workers is the next step for large
  or slow inputs; background tasks alone would not provide durable job recovery.
- **Explicit projection limits:** a documented local strategy is easier to verify.
  Geodesic measurement or tiled/equal-area approaches would support wider regions,
  but require additional policies and tests.

## Learning and future scope

The key implementation lessons are that coordinates need a known CRS, axis order
must be explicit, planar operations require a suitable projection, and a successful
upload can contain features that cannot be measured. File validation and numerical
validation are separate responsibilities.

Future work could add durable processing jobs, PostgreSQL/PostGIS, object storage,
retention/deletion APIs, authenticated ownership, geodesic/global measurement
options, richer KML support, CRS transformation quality reporting, and operational
metrics.
