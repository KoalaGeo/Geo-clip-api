# Geo clip API

pygeoapi plugins that clip data held in PostGIS to a WKT polygon and hand it
back as GeoJSON, plus a Docker image that serves them from the official
pygeoapi base image.

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

The loader skips layers that are already in the database, so only the first
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

| Input | Required | Default | Notes |
| --- | --- | --- | --- |
| `wkt` | yes | | `POLYGON` or `MULTIPOLYGON`. EWKT (`SRID=27700;POLYGON((...))`) is accepted and overrides `srid`. Self-intersecting rings are repaired with `ST_MakeValid`. |
| `table` | yes | | `table` or `schema.table`, as returned by `list-tables` |
| `geometry_column` | no | | only needed for tables with more than one geometry column |
| `srid` | no | `4326` | EPSG code of the clip geometry |
| `output_srid` | no | `4326` | EPSG code of the returned geometries |
| `properties` | no | all columns | array (or comma separated string) of columns to return |
| `limit` | no | `default_limit` | capped by the server's `max_features` |
| `clip` | no | `true` | `false` returns intersecting features whole instead of cutting them at the boundary |

The clip geometry is reprojected into the table's CRS before the spatial
predicate runs, so the table's GiST index is used; the results are then
reprojected to `output_srid`. `numberReturned` and `truncated` are added to the
`FeatureCollection` as foreign members — `truncated` is `true` when the limit
was reached and there may be more data.

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
| `default_limit` | `1000` | features returned when the request sets no `limit` |
| `max_features` | `10000` | hard ceiling on `limit` |
| `statement_timeout` | `60000` | per-query timeout in ms |
| `coordinate_precision` | `7` | decimal places in the output GeoJSON |
| `pool_min` / `pool_max` | `1` / `5` | connection pool size |
| `make_valid_source` | `false` | set `true` if the source geometries are not OGC valid |

Environment variables used by the shipped config: `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`GEOCLIP_SCHEMA`, `GEOCLIP_DEFAULT_LIMIT`, `GEOCLIP_MAX_FEATURES`,
`GEOCLIP_STATEMENT_TIMEOUT`, `PYGEOAPI_SERVER_URL`, `PYGEOAPI_LOGLEVEL`.

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
geoclip/geometry.py               WKT/EWKT parsing and validation
geoclip/processes/clip.py         ClipProcessor
geoclip/processes/list_tables.py  ListTablesProcessor
geoclip/processes/common.py       config, input unwrapping, error mapping
pygeoapi-config.yml               pygeoapi configuration wiring both plugins
docker/entrypoint.sh              creates the process manager paths, then
                                  hands over to the pygeoapi entrypoint
docker/load-geopackage.sh         ogr2ogr load of a GeoPackage into PostGIS
docker/initdb/                    demo data for docker compose
```

## Known limitations

* only `geometry` columns are listed; `geography` columns are not (cast them
  in a view if you need them);
* tables whose geometry column has SRID 0 are clipped on the assumption that
  the clip geometry uses the same CRS, and their output is not reprojected;
* `truncated` reports that the limit was reached, not how many features were
  left behind.
