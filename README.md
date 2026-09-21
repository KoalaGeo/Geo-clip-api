# Geo clip API

pygeoapi plugins that clip data held in PostGIS to a WKT polygon and hand it
back as GeoJSON, plus a Docker image that serves them from the official
pygeoapi base image.

There are two ways to serve it, over one clipping library:

| | `geoclip.processes` (pygeoapi) | `geoclip.api` (FastAPI) |
| --- | --- | --- |
| Speaks | OGC API - Processes | plain HTTP, download shaped |
| Good for | an interactive, standards-conformant API | selling and delivering files |
| Image | `Dockerfile` (pygeoapi base, ~1 GB) | `Dockerfile.api` (python slim) |
| Port in compose | 5000 | 5001 |

The **[download service](#download-service)** exists because a process
response cannot set `Content-Disposition`, its generated OpenAPI can only
declare one media type, and its job manager does not survive a second
replica. Same library, same validation, same formats.

Two [pygeoapi process plugins](https://docs.pygeoapi.io/en/latest/plugins.html)
(OGC API - Processes) are published:

| Process | What it does |
| --- | --- |
| `list-tables` | lists the spatial tables that can be clipped, with geometry column, geometry type, SRID and an estimated row count |
| `clip` | clips one of those tables to a `POLYGON`/`MULTIPOLYGON` given as WKT and returns a GeoJSON `FeatureCollection` |

They are two processes rather than one because OGC API - Processes gives each
process its own description, input schema and job; a single process with a
"mode" input would hide the table catalogue from the generated OpenAPI
document. Both share one code base and one database configuration.

## Quick start

```bash
docker compose up --build        # PostGIS + GeoPackage load + the API on :5000
```

Three stages: PostGIS with the small demo tables, a GDAL stage that pushes
`tests/*.gpkg` into it with `ogr2ogr`, then the API once the load finishes.

```bash
# what can I clip?
curl -s -X POST http://localhost:5000/processes/list-tables/execution \
  -H 'Content-Type: application/json' -d '{"inputs":{}}'

# clip it
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.boreholes",
      "wkt": "POLYGON((-3.25 55.92, -3.10 55.92, -3.10 56.00, -3.25 56.00, -3.25 55.92))"
    }
  }'
```

The response is a GeoJSON `FeatureCollection`:

```json
{
  "type": "FeatureCollection",
  "features": [
    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [-3.19, 55.95]},
     "properties": {"id": 1, "name": "BH001", "depth_m": 42.5}}
  ],
  "numberReturned": 1,
  "truncated": false
}
```

Process metadata lives at `/processes/clip` and `/processes/list-tables`, and
the whole API is described at `/openapi`.

## Download service

`geoclip.api` is a FastAPI application over the same library, with no
pygeoapi in it. `docker compose up` serves it on port 5001; on its own:

```bash
pip install -e ".[api,formats]"
POSTGRES_HOST=localhost POSTGRES_DB=geodata \
  uvicorn geoclip.api:app --port 8000
```

| Endpoint | What it does |
| --- | --- |
| `GET /tables`, `GET /tables/{name}` | what can be clipped |
| `POST /estimate`, `GET /estimate` | rows, covered area and download size, without building it |
| `POST /clip`, `GET /clip` | the download |
| `GET /healthz`, `GET /readyz` | liveness (no database) and readiness (database) |
| `GET /docs`, `GET /openapi.json` | the API description |

`POST` takes a JSON body and accepts any clip area, including a drawn
GeoJSON `FeatureCollection`. `GET` takes query parameters and so is
limited to a bbox or a WKT polygon short enough to survive a URL — but it
gives you a **download as a link**:

```html
<a href="http://localhost:5001/clip?table=public.625k_v5_bedrock_geology&bbox=-3.30,55.90,-3.05,56.02&format=gpkg"
   download>Download this area</a>
```

No JavaScript, no `Blob`, and the file lands with the name the service
sends. It is also cacheable, and `FileResponse` serves Range requests, so a
FlatGeobuf URL can be read directly by the fgb client in MapLibre or
OpenLayers.

What it does that the process cannot:

* **names the file** — `Content-Disposition: attachment;
  filename="625k_v5_bedrock_geology_20260920T173519Z.gpkg"`;
* **negotiates content** — `format` in the body, or an `Accept` header of
  `application/geo+json`, `application/geopackage+sqlite3` or
  `application/flatgeobuf`;
* **refuses an order it cannot fill** — the default `on_limit=error`
  answers `413` with the row count and the limit rather than quietly
  returning the first N features. `on_limit=truncate` restores the old
  behaviour, and every response carries `X-Geoclip-Rows` and
  `X-Geoclip-Truncated`;
* **declares all three media types in its OpenAPI**, so a browser console
  offers a download instead of printing a GeoPackage as text;
* **errors as `application/problem+json`** (RFC 9457) with `400`, `404`,
  `413` and `503` meaning what they should.

```bash
# what would this order contain?
curl -s -X POST http://localhost:5001/estimate \
  -H 'Content-Type: application/json' -d '{
    "table": "public.625k_v5_bedrock_geology",
    "bbox": [-3.30, 55.90, -3.05, 56.02],
    "format": "gpkg"
  }'
# {"table":"public.625k_v5_bedrock_geology","rows":15,"vertices":444,
#  "requested_area_km2":208.62,"covered_area_km2":123.94,"format":"gpkg",
#  "estimated_bytes":98304,"limit":1000,"within_limit":true}

# take it
curl -s -X POST http://localhost:5001/clip \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/flatgeobuf' -OJ -d '{
    "table": "public.625k_v5_bedrock_geology",
    "bbox": [-3.30, 55.90, -3.05, 56.02],
    "simplify": true
  }'
```

`curl -OJ` uses the filename the service sends.

### The API console

`/docs` is the usual Swagger UI. On the `GET` operations `format`,
`on_limit` and `clip` render as drop-downs, because a drop-down comes from
an enum *parameter*; a JSON request body is always a text area, whatever
its schema says. That is the practical reason to have the `GET` twins at
all beyond linkable downloads.

FastAPI loads the console's JavaScript from a CDN, which a cluster without
egress cannot reach — the page renders empty. Point it at an internal copy
with `GEOCLIP_SWAGGER_JS_URL` and `GEOCLIP_SWAGGER_CSS_URL`; the assets
must be Swagger UI 5 or later, since the document is OpenAPI 3.1.

### Estimating

`POST /estimate` answers the two questions a shop asks — how many rows, and
how much area with data in it — plus a size prediction, in one pass that
costs a fraction of the clip:

| Order (1:625k bedrock) | Estimate | The clip itself |
| --- | --- | --- |
| 15 features, 209 km² | 0.05 s | 0.3 s |
| 3,486 features, 110,000 km² | 1.4 s | 3.8 s |

The size model is rows × property bytes + vertices × 24, with the vertex
count scaled by how much of each feature survives the clip; measured
against real clips it lands within about 20%. `covered_area_km2` sums the
clipped areas, which double counts where source features overlap each
other — `exact_area: true` unions them instead, about three times slower.
Point and line layers have no area, so price those by row.

## Test data

Two sets of data land in PostGIS:

* `docker/initdb/01-demo-data.sql` — two tiny tables (`boreholes`, `bedrock`)
  created when the database is first initialised;
* `tests/625k_V5_Geology_UK_EPSG27700.gpkg` — the 1:625k UK geology
  GeoPackage, loaded by the `gpkg-loader` compose stage
  (`ghcr.io/osgeo/gdal:alpine-small-latest`, which carries both the GPKG and
  PostgreSQL drivers).

Each spatial layer becomes a table under its laundered name, so after the
first `docker compose up` the clip process can be pointed at:

| Table | Geometry | SRID | Features |
| --- | --- | --- | --- |
| `public.625k_v5_bedrock_geology` | MULTIPOLYGON | 27700 | 11244 |
| `public.625k_v5_superficial_geology` | MULTIPOLYGON | 27700 | 10651 |
| `public.625k_v5_dykes_geology` | MULTIPOLYGON | 27700 | 3263 |
| `public.625k_v5_faults` | MULTILINESTRING | 27700 | 2741 |

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "wkt": "POLYGON((-3.30 55.90, -3.05 55.90, -3.05 56.02, -3.30 56.02, -3.30 55.90))",
      "properties": ["lex_d", "rcs_d", "max_period"]
    }
  }'
```

Attribute tables (QGIS' `layer_styles`) and driver-internal tables are not
loaded, and layers already in the database are skipped, so only the first
run pays for the import. It is driven by environment variables on the
`gpkg-loader` service:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GPKG_PATH` | `/data/625k_V5_Geology_UK_EPSG27700.gpkg` | GeoPackage to load (`./tests` is mounted at `/data`) |
| `GPKG_LAYERS` | every spatial layer | space separated subset to load |
| `GPKG_SCHEMA` | `public` | target schema |
| `GPKG_SRS` | `EPSG:27700` (set in compose) | CRS assigned on load with `-a_srs`, no reprojection |
| `GPKG_FORCE` | `false` | `true` reloads layers that already exist |
| `GDAL_IMAGE` | `ghcr.io/osgeo/gdal:alpine-small-latest` | image used for the load |

`GPKG_SRS` matters for this file: three of its four layers reference a
private SRS id (`100000`) rather than an EPSG code, and without the override
they would land in PostGIS with an unknown SRID — which stops the clip
process reprojecting them. To load your own data instead, drop the file in
`tests/` and set `GPKG_PATH` (and `GPKG_SRS`, if its CRS is not declared
with an EPSG code).

## Inputs

### `clip`

The clip area comes from exactly one of `wkt`, `geometry` or `bbox`.

| Input | Required | Default | Notes |
| --- | --- | --- | --- |
| `wkt` | one of three | | `POLYGON` or `MULTIPOLYGON`. EWKT (`SRID=27700;POLYGON((...))`) is accepted and overrides `srid`. Self-intersecting rings are repaired with `ST_MakeValid`. |
| `geometry` | one of three | | GeoJSON Polygon/MultiPolygon, Feature, or FeatureCollection (features are merged into one area) — what a Leaflet, OpenLayers or MapLibre drawing control gives you |
| `bbox` | one of three | | `[minx, miny, maxx, maxy]` in the `srid` CRS, for clipping to a map viewport |
| `table` | yes | | `table` or `schema.table`, as returned by `list-tables` |
| `geometry_column` | no | | only needed for tables with more than one geometry column |
| `srid` | no | `4326` | EPSG code of the clip geometry |
| `output_srid` | no | `4326` | EPSG code of the returned geometries |
| `properties` | no | all columns | array (or comma separated string) of columns to return |
| `limit` | no | `default_limit` | capped by the server's `max_features` |
| `clip` | no | `true` | `false` returns intersecting features whole instead of cutting them at the boundary |
| `simplify` | no | `false` | `true` generalises the output with `ST_SimplifyPreserveTopology`, tolerance ≈ one pixel of the clip extent on a 2000px map; a number sets the tolerance explicitly, in output CRS units |
| `format` | no | `geojson` | `gpkg` or `fgb` return a file instead (see below) |

The clip geometry is reprojected into the table's CRS before the spatial
predicate runs, so the table's GiST index is used; the results are then
reprojected to `output_srid`. `numberReturned` and `truncated` are added to the
`FeatureCollection` as foreign members — `truncated` is `true` when the limit
was reached and there may be more data.

### Clip area examples

Every example below runs against the `docker compose` stack as it comes up,
and the feature counts are what it returns.

**WKT** — for humans, QGIS and the command line:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.boreholes",
      "wkt": "POLYGON((-3.25 55.92, -3.10 55.92, -3.10 56.00, -3.25 56.00, -3.25 55.92))"
    }
  }'                                                        # 2 features
```

**EWKT** — the `SRID=` prefix sets the CRS of the clip area, so you can cut
with a British National Grid polygon and still get WGS84 back:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "wkt": "SRID=27700;POLYGON((320000 670000, 340000 670000, 340000 680000, 320000 680000, 320000 670000))",
      "properties": ["lex_d"]
    }
  }'                                                       # 10 features
```

**GeoJSON geometry** — what `layer.toGeoJSON()` and
`GeoJSON().writeGeometryObject()` give you:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.boreholes",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[-3.25, 55.92], [-3.10, 55.92], [-3.10, 56.00],
                         [-3.25, 56.00], [-3.25, 55.92]]]
      }
    }
  }'                                                        # 2 features
```

**GeoJSON FeatureCollection** — post `draw.getAll()` straight through; two
drawn boxes are merged into one area of interest:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.boreholes",
      "geometry": {
        "type": "FeatureCollection",
        "features": [
          {"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon",
            "coordinates": [[[-3.25, 55.92], [-3.15, 55.92], [-3.15, 56.00],
                             [-3.25, 56.00], [-3.25, 55.92]]]}},
          {"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon",
            "coordinates": [[[-2.95, 56.05], [-2.85, 56.05], [-2.85, 56.15],
                             [-2.95, 56.15], [-2.95, 56.05]]]}}
        ]
      }
    }
  }'                    # 3 features: BH001 and BH002 from the first box,
                        # BH003 from the second
```

**bbox** — clip to the map view, here with the guard rails a web map wants:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "bbox": [-3.30, 55.90, -3.05, 56.02],
      "srid": 4326,
      "simplify": true,
      "properties": ["lex_d", "max_period"]
    }
  }'                                                       # 15 features
```

### Download formats

| `format` | Media type | Notes |
| --- | --- | --- |
| `geojson` (default) | `application/geo+json` | a `FeatureCollection` in the response body |
| `gpkg` | `application/geopackage+sqlite3` | GeoPackage, one layer named after the source table; what QGIS and ArcGIS want |
| `fgb` | `application/flatgeobuf` | FlatGeobuf: same data, far smaller, streams into OpenLayers and MapLibre |

`geopackage` and `flatgeobuf` are accepted as aliases.

The same clip in each format — 15 features of the bedrock layer, all
attributes:

```bash
# GeoJSON: the default, straight into a map or jq
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "bbox": [-3.30, 55.90, -3.05, 56.02]
    }
  }' -o bedrock.geojson

# GeoPackage: open it in QGIS
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "bbox": [-3.30, 55.90, -3.05, 56.02],
      "format": "gpkg"
    }
  }' -o bedrock.gpkg                                          # ~116 kB

# FlatGeobuf: the same data for the browser
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_bedrock_geology",
      "bbox": [-3.30, 55.90, -3.05, 56.02],
      "format": "fgb"
    }
  }' -o bedrock.fgb                                            # ~25 kB
```

`output_srid` applies to the file too, so this one opens in QGIS as British
National Grid:

```bash
curl -s -X POST http://localhost:5000/processes/clip/execution \
  -H 'Content-Type: application/json' -d '{
    "inputs": {
      "table": "public.625k_v5_faults",
      "bbox": [-3.30, 55.90, -3.05, 56.02],
      "format": "gpkg",
      "output_srid": 27700
    }
  }' -o faults-bng.gpkg
```

The files are written with OGR (through fiona, which the pygeoapi image
ships as `python3-fiona`); a server without it still serves GeoJSON and
refuses the other two with a clear error.

**Swagger UI cannot show you these.** The `/openapi?f=html` console prints
*Unrecognized response type; displaying content as text* and dumps the bytes,
which look like `SQLite format 3...GPKG...`. That is the console, not the
server: pygeoapi declares a single media type for a process response (it
takes the first output's `contentMediaType`, defaulting to
`application/json`), so a reply of `application/geopackage+sqlite3` is a type
the page was never told about. The body is a valid file — save it and it
opens in QGIS. Use `curl -o`, or in a browser:

```js
const response = await fetch(`${API}/processes/clip/execution`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({inputs: {table: TABLE, bbox: bbox, format: 'gpkg'}})
});
const url = URL.createObjectURL(await response.blob());
Object.assign(document.createElement('a'),
              {href: url, download: 'clip.gpkg'}).click();
URL.revokeObjectURL(url);
```

On Windows, PowerShell mangles the quoting in the `curl` examples above, so
either use `Invoke-WebRequest`:

```powershell
$body = @{
  inputs = @{
    table  = 'public.boreholes'
    bbox   = @(-3.20, 55.94, -3.15, 55.97)
    format = 'gpkg'
  }
} | ConvertTo-Json -Depth 5

Invoke-WebRequest -Uri 'http://localhost:5000/processes/clip/execution' `
  -Method Post -ContentType 'application/json' -Body $body `
  -OutFile boreholes.gpkg

# it is a GeoPackage if these say "SQLite format 3" and "GPKG"
$bytes = [System.IO.File]::ReadAllBytes("$PWD\boreholes.gpkg")
[Text.Encoding]::ASCII.GetString($bytes[0..14])
[Text.Encoding]::ASCII.GetString($bytes[68..71])
```

or keep the body in a file and hand it to the real `curl.exe`:

```powershell
'{"inputs":{"table":"public.boreholes","bbox":[-3.20,55.94,-3.15,55.97],"format":"gpkg"}}' `
  | Set-Content request.json -Encoding utf8

curl.exe -s -X POST http://localhost:5000/processes/clip/execution `
  -H "Content-Type: application/json" --data-binary "@request.json" `
  -o boreholes.gpkg
```

To look inside it without installing GDAL, use the image compose already
pulls:

```powershell
docker run --rm -v "${PWD}:/data" ghcr.io/osgeo/gdal:alpine-small-latest `
  ogrinfo -so /data/boreholes.gpkg boreholes
```

Worth knowing before you wire up a download button:

* the file carries the **output** CRS, so `output_srid` applies to it as
  well; a source table with SRID 0 produces a file without a CRS;
* pygeoapi gives a process no control over response headers, so there is no
  `Content-Disposition` — the browser names the download after the URL
  unless you set the name yourself (`<a download="bedrock.gpkg">`, or
  `-o` with curl);
* binary formats need the default raw response. Asking for
  `"response": "document"` wraps the output in JSON, which bytes cannot go
  into;
* use async (`Prefer: respond-async`) for large exports and collect the file
  from `/jobs/{id}/results`;
* `numberReturned` and `truncated` live in the GeoJSON response, not in the
  file, so a `limit` that was hit is invisible in a download — check the
  feature count, or ask for GeoJSON first;
* attribute columns come out in PostgreSQL's `jsonb` key order rather than
  the table's column order, booleans are written as `0`/`1` (OGR via fiona
  has no boolean field type), and FlatGeobuf reorders features into its
  spatial index.

### From a web map

`bbox` and `geometry` exist so a browser never has to build WKT. CORS is on,
and the sync response is a GeoJSON `FeatureCollection` you can hand straight
to a layer.

```js
// Leaflet: clip to the current view
const b = map.getBounds();
const response = await fetch(`${API}/processes/clip/execution`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({inputs: {
    table: 'public.625k_v5_bedrock_geology',
    bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()],
    simplify: true
  }})
});
L.geoJSON(await response.json()).addTo(map);
```

```js
// MapLibre + mapbox-gl-draw: clip to whatever the user drew
body: JSON.stringify({inputs: {
  table: 'public.625k_v5_bedrock_geology',
  geometry: draw.getAll(),        // a FeatureCollection; parts are merged
  simplify: true
}})
```

```js
// OpenLayers: a drawn geometry, reprojected to WGS84 first
const geojson = new GeoJSON().writeGeometryObject(feature.getGeometry(), {
  dataProjection: 'EPSG:4326',
  featureProjection: map.getView().getProjection()
});
body: JSON.stringify({inputs: {table: TABLE, geometry: geojson}})
```

Send `srid` if your coordinates are not WGS84 (an OpenLayers geometry left in
the map's own projection is usually `3857`), and remember the response
carries `numberReturned` and `truncated` — show the user something when the
limit was hit rather than silently plotting a partial layer.

`simplify: true` is worth having on for display: the tolerance is derived
from the size of the area asked for, so a viewport-sized clip is generalised
to roughly a pixel while a field-sized one is left alone. Leave it off when
the response is going into an analysis or a download.

### `list-tables`

| Input | Required | Notes |
| --- | --- | --- |
| `schema` | no | restrict to one published schema |
| `match` | no | case-insensitive substring of the qualified table name |

Both processes support `sync-execute`; `clip` also supports `async-execute`
(send `Prefer: respond-async` and poll `/jobs/{id}/results`), which is the
better choice for large areas of interest.

## Configuration

`pygeoapi-config.yml` wires both plugins in by dotted path and shares one
`data` block between them via a YAML anchor:

```yaml
resources:
    clip:
        type: process
        processor:
            name: geoclip.processes.clip.ClipProcessor
            data:
                host: ${POSTGRES_HOST:-postgres}
                dbname: ${POSTGRES_DB:-geodata}
                user: ${POSTGRES_USER:-postgres}
                password: ${POSTGRES_PASSWORD:-postgres}
                allowed_schemas: [public]
```

| Key | Default | Purpose |
| --- | --- | --- |
| `host` / `port` / `dbname` / `user` / `password` | `POSTGRES_*` or `PG*` env vars | connection details |
| `dsn` | `GEOCLIP_DSN` / `DATABASE_URL` | libpq connection string, instead of the above |
| `allowed_schemas` | `[public]` | only these schemas are listed or clipped; system schemas are always refused |
| `allowed_tables` | all | optional whitelist (`public.boreholes` or `boreholes`) |
| `excluded_tables` | none | optional blacklist; wins over the whitelist |

The three list settings accept either a YAML list or a comma separated
string, because pygeoapi expands `${VAR}` to a scalar — an environment
variable can never produce a YAML list.
| `default_limit` | `1000` | features returned when the request sets no `limit` |
| `max_features` | `10000` | hard ceiling on `limit` |
| `statement_timeout` | `60000` | per-query timeout in ms |
| `coordinate_precision` | `7` | decimal places in the output GeoJSON |
| `simplify_divisor` | `2000` | `simplify: true` uses the clip extent divided by this as the tolerance |
| `pool_min` / `pool_max` | `1` / `5` | connection pool size |
| `make_valid_source` | `false` | set `true` if the source geometries are not OGC valid |

Every setting in the shipped config is driven by an environment variable,
so the published image is configured without editing YAML inside it:

| Variable | Sets |
| --- | --- |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | connection |
| `GEOCLIP_DSN` or `DATABASE_URL` | libpq connection string; **takes precedence over the `POSTGRES_*` values** |
| `GEOCLIP_SCHEMAS` | published schemas, comma separated |
| `GEOCLIP_ALLOWED_TABLES` | whitelist, comma separated; empty means every table in those schemas |
| `GEOCLIP_EXCLUDED_TABLES` | blacklist, comma separated |
| `GEOCLIP_DEFAULT_LIMIT` / `GEOCLIP_MAX_FEATURES` | feature limits |
| `GEOCLIP_STATEMENT_TIMEOUT` | per-query timeout (ms) |
| `GEOCLIP_COORDINATE_PRECISION` / `GEOCLIP_POOL_MAX` / `GEOCLIP_MAKE_VALID_SOURCE` | output precision, pool size, `ST_MakeValid` on source geometries |
| `GEOCLIP_SIMPLIFY_DIVISOR` | tolerance divisor used by `simplify: true` |
| `PYGEOAPI_SERVER_URL` / `PYGEOAPI_LOGLEVEL` | pygeoapi's own URL and logging |
| `GEOCLIP_PROCESS_OUTPUT_DIR` / `GEOCLIP_JOB_DB` | process manager paths (see below) |

`tests/test_config.py` asserts this: it loads `pygeoapi-config.yml` through
pygeoapi with those variables set and checks what the plugins end up with.

To publish different tables, mount your own config over
`/pygeoapi/local.config.yml` (or point `PYGEOAPI_CONFIG` elsewhere).

### Process manager paths

pygeoapi's process manager writes every job's output to a file under its
`output_dir` and does **not** create that directory, so a server started
without it fails each execution with:

```
Error executing process: [Errno 2] No such file or directory:
  '/tmp/pygeoapi-process-outputs/clip-<job id>'
```

In Docker, `docker/entrypoint.sh` creates the manager's paths before
pygeoapi starts — it reads them from the running configuration, so a mounted
config with different paths is handled too. The shipped defaults are
`GEOCLIP_PROCESS_OUTPUT_DIR=/tmp/pygeoapi-process-outputs` and
`GEOCLIP_JOB_DB=/tmp/pygeoapi-process-manager.db`. Outside Docker, create
them yourself (see below).

## Safety

* table, schema, geometry column and property names are checked against
  `^[A-Za-z_][A-Za-z0-9_$]*$`, looked up in `geometry_columns`, and then
  composed with `psycopg2.sql.Identifier` — request values never reach SQL as
  text;
* the WKT and every other value are sent as bound parameters;
* connections are opened `READ ONLY` with a statement timeout, and only
  schemas listed in `allowed_schemas` are visible;
* give the API a database role with `SELECT` on the tables you want published
  and nothing more.

## Docker

`Dockerfile` builds on `geopython/pygeoapi:latest`, installs the `geoclip`
package into the image's virtualenv with `--no-deps` (the base image already
carries psycopg2 and shapely), copies `pygeoapi-config.yml` to
`/pygeoapi/local.config.yml`, and checks at build time that both plugins
import.

Published images are built and pushed to GHCR by the `publish` CI job on
every push to `main` and every `v*` tag (and on demand from the Actions tab),
but only after the unit tests and the container smoke test pass:

```bash
docker run --rm -p 5000:80 \
  -e PYGEOAPI_SERVER_URL=http://localhost:5000 \
  -e POSTGRES_HOST=db.example.org -e POSTGRES_DB=geodata \
  -e POSTGRES_USER=reader -e POSTGRES_PASSWORD=secret \
  -e GEOCLIP_SCHEMAS=public,geology \
  -e GEOCLIP_ALLOWED_TABLES=public.boreholes,geology.625k_v5_bedrock_geology \
  ghcr.io/koalageo/geo-clip-api:latest
```

Tags: `latest` (default branch), the branch name, `sha-<commit>`, and
`1.2` / `1.2.3` for `v*` tags. GHCR packages start out private — publish
the package from its GitHub page if the image should be pullable
anonymously. To build it yourself instead:

```bash
docker build -t geo-clip-api .
docker run --rm -p 5000:80 \
  -e PYGEOAPI_SERVER_URL=http://localhost:5000 \
  -e POSTGRES_HOST=db.example.org -e POSTGRES_DB=geodata \
  -e POSTGRES_USER=reader -e POSTGRES_PASSWORD=secret \
  geo-clip-api
```

### Windows checkouts

The shell scripts run inside Linux containers but are read from the host
checkout, so CRLF line endings break them:

```
/load-geopackage.sh: line 14: : not found
/load-geopackage.sh: set: line 15: illegal option -
```

`.gitattributes` keeps every text file LF regardless of `core.autocrlf`, the
image strips CR from its entrypoint at build time, and the compose loader
strips CR before running. A checkout made *before* `.gitattributes` existed
keeps its CRLF files until git rewrites them, which the loader tolerates; to
normalise the working tree anyway (this discards uncommitted changes):

```bash
git rm --cached -r .
git reset --hard
```

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
pytest                 # unit tests, no database needed
flake8 geoclip tests
```

Integration tests run against a real PostGIS loaded with
`docker/initdb/01-demo-data.sql` and are skipped unless a DSN is given:

```bash
docker compose up -d postgres
GEOCLIP_TEST_DSN=postgresql://postgres:postgres@localhost:5432/geodata pytest
```

To run the server outside Docker, point `PYGEOAPI_OGC_SCHEMAS_LOCATION` at
`http://schemas.opengis.net` (the default path only exists inside the image):

```bash
export PYGEOAPI_CONFIG=pygeoapi-config.yml PYGEOAPI_OPENAPI=/tmp/openapi.yml
export PYGEOAPI_OGC_SCHEMAS_LOCATION=http://schemas.opengis.net
export POSTGRES_HOST=localhost POSTGRES_DB=geodata
mkdir -p /tmp/pygeoapi-process-outputs   # the process manager needs this
pygeoapi openapi generate $PYGEOAPI_CONFIG --output-file $PYGEOAPI_OPENAPI
pygeoapi serve
```

## Layout

```
geoclip/db.py                     PostGIS access: catalogue + clip SQL
geoclip/formats.py                GeoPackage and FlatGeobuf writers
geoclip/geometry.py               WKT/GeoJSON/bbox clip area parsing
geoclip/processes/clip.py         ClipProcessor
geoclip/processes/list_tables.py  ListTablesProcessor
geoclip/processes/common.py       config, input unwrapping, error mapping
pygeoapi-config.yml               pygeoapi configuration wiring both plugins
docker/entrypoint.sh              creates the process manager paths, then
                                  hands over to the pygeoapi entrypoint
docker/load-geopackage.sh         ogr2ogr load of a GeoPackage into PostGIS
docker/initdb/                    demo data for docker compose
```

## Running it behind a shop

`BIGCOMMERCE.md` has the integration notes for using this as the engine of a
data shop: the queue/worker shape on Kubernetes, why pygeoapi's own async
jobs do not survive more than one replica, and how to price an order by row
count or by area without doing the work twice.

## Known limitations

* only `geometry` columns are listed; `geography` columns are not (cast them
  in a view if you need them);
* tables whose geometry column has SRID 0 are clipped on the assumption that
  the clip geometry uses the same CRS, and their output is not reprojected;
* `truncated` reports that the limit was reached, not how many features were
  left behind.
