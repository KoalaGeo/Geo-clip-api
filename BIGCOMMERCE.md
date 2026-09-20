# Running this behind a BigCommerce data shop

Notes for using the clip API as the engine of a geospatial data shop: a
customer buys an area of a dataset, and gets a file.

**Nothing described here is implemented in this repository.** What exists
today is the clip and list-tables processes, the container, and the
configuration around them. This document records the design decisions and
the measurements behind them so the shop side can be built without
rediscovering them. The last section lists what is missing.

## Shape

```
BigCommerce ──webhook──▶ Redis ──▶ worker ──HTTP──▶ pygeoapi (k8s)
   order                queue     (RQ/Celery)        clip process
     ▲                              │                     │
     └──── signed URL ──── artifact store ◀───────────────┘
                          (S3/Azure Blob)
```

pygeoapi is a stateless compute service. Redis owns the queue, the worker
owns retries and order state, and the artifact store owns the file. Keep
pygeoapi's own async jobs out of it: its process manager stores job state in
a file inside the pod and writes output to a pod-local directory, so with
more than one replica `/jobs/{id}` polls land on a random replica and 404.
Call `/processes/clip/execution` synchronously from the worker and let the
queue do what queues do.

## Two deployments of the same image

| | Quote | Fulfilment |
| --- | --- | --- |
| Serves | basket, price preview, map browsing | paid orders |
| Typical call | count and area only | the full clip |
| `GEOCLIP_MAX_FEATURES` | low (say 1000) | the real ceiling |
| `GEOCLIP_STATEMENT_TIMEOUT` | 5000 | minutes, deliberately |
| Replicas | many, small | fewer, more memory |
| Ingress timeout | short | matched to the statement timeout |

Splitting them means a customer buying all of Wales cannot starve the browse
API, and the two have genuinely different timeout and memory profiles.

Both need `WSGI_WORKER_CLASS=sync` (or `gthread`). The image defaults to
gevent workers, and psycopg2 blocks the event loop — neither pygeoapi nor
the image applies `psycogreen`, so a running clip stalls every other request
on that worker, including its readiness probe, which can get the pod
restarted mid-job. Scale with replicas instead.

`/processes` is a safe readiness probe: the plugins load without connecting
to PostgreSQL (the pool is lazy), so the probe proves the app is up without
flapping when the database blips.

## Pricing

Both pricing models need a number *before* doing the work, and the work is
the expensive part. Measured on the 1:625k bedrock layer (11,244 polygons)
in a container, for a Scotland-sized order of about 110,000 km²:

| Query | Result | Time |
| --- | --- | --- |
| exact row count | 3,486 rows | 436 ms |
| planner estimate | 1,250 rows | 0.7 ms |
| clip area | 109,560 km² | 0.4 ms |
| the actual clip | 11.7 MB of GeoJSON | 1,460 ms |

So an exact quote costs roughly a third of the delivery and returns no data.
Quote honestly; do not price on the planner estimate — it was **2.8x under**
the real count here, because PostGIS bounding-box selectivity is a guess.
Use it only as a tier hint ("this looks large") if you want an instant
answer while the exact count runs.

### Per row

```sql
SELECT count(*)
FROM public."625k_v5_bedrock_geology" t,
     (SELECT ST_Transform(ST_SetSRID(ST_GeomFromText($1), $2), 27700) g) c
WHERE t.geom && c.g AND ST_Intersects(t.geom, c.g);
```

Same predicate as the clip, without `ST_Intersection` or the JSON building —
that is where the saving comes from. It uses the GiST index.

### Per area

Charge for the area that has data in it, not the area the customer drew:

```sql
-- what they asked for: 208.49 km2
SELECT ST_Area(ST_Transform(ST_SetSRID(ST_GeomFromText($1), $2), 27700)) / 1e6;

-- what they would actually receive: 123.86 km2
SELECT ST_Area(ST_Union(ST_Intersection(t.geom, c.g))) / 1e6
FROM public."625k_v5_bedrock_geology" t,
     (SELECT ST_Transform(ST_SetSRID(ST_GeomFromText($1), $2), 27700) g) c
WHERE t.geom && c.g AND ST_Intersects(t.geom, c.g);
```

On the Edinburgh example in the main README the drawn box is **68% larger**
than the area that has data in it (208.49 km² against 123.86 km²) — about
40% of that box is the Firth of Forth. Billing on the drawn box overcharges
every coastal order, which in the UK is most of them.

Area must be computed in a projected CRS — EPSG:27700 gives square metres
for British data. Do not use degrees. For data outside the BNG area, cast to
`geography` or use an equal-area projection.

### Things that will bite

* **`truncated` is a billing bug.** The clip process caps at
  `max_features` and reports `truncated: true`; a paying customer must never
  receive a quietly partial file. Have the worker fail the job when the
  count exceeds the cap, and price large orders deliberately rather than
  silently clipping them short.
* **The quote and the delivery must agree.** Cache the quote against the
  same idempotency key the fulfilment uses (see below). A price that moves
  between basket and delivery is a support ticket, and the data can change
  underneath a slow checkout.
* **Charge on the clipped geometry, not the source rows.** A polygon that
  barely overlaps the order still counts as a row; if that matters for
  fairness, price on area, or on the area of the intersection per row.

## Order lifecycle

1. **Quote** — basket calls the quote deployment: count, area, estimated
   file size. Cache it under the idempotency key.
2. **Order paid** — BigCommerce webhook onto Redis. Webhooks retry, so the
   job must be idempotent: key on a hash of table, clip geometry, srid,
   output srid, format, properties, limit and simplify. A repeat of the same
   key returns the artifact already built.
3. **Fulfil** — worker calls the fulfilment deployment, uploads the bytes to
   the artifact store, records the key.
4. **Deliver** — a signed URL with a TTL against the order. BigCommerce
   digital products accept a URL, so the artifact store is the source of
   truth, not the shop.

The file comes back in the HTTP response body, so the worker holds it in
memory before uploading. That is fine for the sizes a product should sell;
if orders get into the hundreds of megabytes, stream `ogr2ogr` directly from
PostGIS to the format instead — `docker/load-geopackage.sh` already shows
that pattern in the opposite direction.

## Catalogue and safety

The clip process will read any table in `GEOCLIP_SCHEMAS`. A shop must not
pass a customer-supplied table name through: map **SKU to table**, CRS and
default format in the product catalogue, and keep `GEOCLIP_ALLOWED_TABLES`
as a backstop so a bug in the mapping cannot sell something unlisted.

Give the fulfilment deployment a database role with `SELECT` on the saleable
tables and nothing else.

## Capacity

* Connections are `replicas x WSGI_WORKERS x pool_max`. Put pgbouncer in
  front, or keep `GEOCLIP_POOL_MAX` small.
* `GEOCLIP_MAX_FEATURES` is the practical memory control: the collection is
  materialised in Python before the file is written. Size pod memory from a
  test at the cap, not from a small example.
* Align the timeout chain — `GEOCLIP_STATEMENT_TIMEOUT` < gunicorn
  (`WSGI_WORKER_TIMEOUT`, 6000s in the base image) <= ingress
  (nginx-ingress defaults to 60s, Istio to 15s) <= the worker's job timeout.
  The ingress default is the one that usually truncates a long export.

## Prior art

Worth knowing what already exists before extending this, and where the ideas
came from.

**GeoServer clips, but not through WFS.** WFS GetFeature offers `bbox` and
`cql_filter` — selection, not clipping — which is the usual reason people
conclude it cannot do this. Clipping lives elsewhere:

* [`gs:Clip`](https://geoserver.geosolutionsgroup.com/edu/en/wps/vector_processes.html)
  (WPS) clips a FeatureCollection against a polygon, with the polygon in the
  layer's native CRS;
* the [WMS `clip` vendor parameter](https://docs.geoserver.org/stable/en/user/services/wms/vendor.html)
  masks rendered output with a WKT polygon;
* the [WPS Download extension](https://docs.geoserver.org/stable/en/user/extensions/wps-download/index.html)
  is the closest existing thing to this shop: it downloads layers as zips,
  clips vectors to an `ROI` with `cropToROI`, writes GeoPackage among other
  [formats](https://docs.geoserver.org/stable/en/user/extensions/wps-download/rawDownload.html),
  and runs asynchronously.

**Steal the estimator.** That module ships `gs:DownloadEstimator` alongside
`gs:Download`: a pre-flight check of how large the answer would be, used to
enforce limits. Somebody else building extraction concluded you must size an
order before you extract it, which is the same conclusion the pricing section
above reaches for a different reason. Build the estimate endpoint.

**MapServer** is selection only for this purpose: it clips to the map extent
when rendering, not to an arbitrary polygon in
[WFS output](https://mapserver.org/ogc/wfs_server.html).

**[pg_featureserv](https://github.com/CrunchyData/pg_featureserv)** is the
minimal-code alternative: publish a PostGIS function as
`/functions/{name}/items.json` with typed parameters, `crs` and `limit`,
[returning GeoJSON](https://access.crunchydata.com/documentation/pg_featureserv/1.3.1/usage/query_function/).
A clip function would be about thirty lines of SQL and no Python. It stops at
GeoJSON, though: no GeoPackage or FlatGeobuf, no process metadata, no jobs,
and the table allow-listing and geometry validation would still need writing
somewhere.

**`ogr2ogr -clipsrc`** is the underlying primitive, and the escape hatch for
orders too large to materialise in memory — stream straight from PostGIS to
the output format. `docker/load-geopackage.sh` already runs ogr2ogr in the
other direction.

**[HOT Export Tool](https://www.hotosm.org/en/tools-resources/tech-product-suite/hot-export-tool/)**
(backend: [hotosm/raw-data-api](https://github.com/hotosm/raw-data-api)) is
the same workflow in the open: draw an area, choose features and formats
(GeoPackage, Shapefile, FlatGeobuf, GeoJSON, KML), submit a job, collect a
download link. OSM-specific data model, but the order to artifact to link
pattern is the one this document describes.

pygeoapi's own `ShapelyFunctions` process is not a substitute: it operates on
geometries the caller supplies, not on a database table.

**Why this repository still exists.** Nothing above does the whole
combination: OGC API - Processes, a PostGIS table clipped by WKT, GeoJSON or
bbox, GeoPackage or FlatGeobuf out, configured entirely by environment
variables, in a container that is not a Java stack. If that stops being true,
or if the shop needs raster extraction as well, GeoServer's WPS Download is
the thing to re-evaluate against — it solves a superset of this problem at
the cost of running GeoServer.

## Still to build

| Piece | Notes |
| --- | --- |
| `estimate` process | count, covered area and estimated file size for the same inputs as `clip`, so quotes go through the same validation |
| Artifact storage | upload, idempotency key, TTL, signed URLs |
| SKU mapping | product to table, CRS, format, price model |
| Fail-on-truncation | refuse rather than under-deliver a paid order |
| Bundle | data + user guide + style, zipped |

### On the bundle

Fetching a user guide and an SLD from network storage and zipping them with
the data is a worker step, not an API one: the worker already has the file
and the order context.

Worth checking before keeping styles on a share: the geology GeoPackage in
`tests/` already carries a `layer_styles` table with both QML and SLD for
every layer (categorised renderers, web-ready hex colours, 244 categories
for bedrock). If those are the styles the shop ships, generating the SLD
from the data removes a copy that can drift. The loader deliberately does
not import that table as a data layer, so it would need reading separately.
