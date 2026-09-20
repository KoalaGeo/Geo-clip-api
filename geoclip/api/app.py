# =================================================================
#
# Geo clip API: standalone download service
#
# =================================================================

"""A download-shaped HTTP API over the clipping library.

The pygeoapi plugins in `geoclip.processes` speak OGC API - Processes,
which is the right shape for an interactive API and the wrong one for
selling files: a process response cannot set `Content-Disposition`, its
OpenAPI document can only declare one media type, and its job manager does
not survive a second replica.

This service is the other adapter over the same library: real content
negotiation, a filename on every download, a limit that refuses an order
rather than quietly truncating it, and an estimate endpoint so a shop can
price one before building it.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from geoclip import __version__
from geoclip.api.models import (ClipRequest, EstimateRequest,
                                EstimateResponse, OnLimit, OutputFormat,
                                Problem, TableInfo, TableList)
from geoclip.db import ClipDatabase, parse_simplify
from geoclip.errors import (DatabaseError, GeoClipError, InvalidInputError,
                            TableNotFoundError)
from geoclip.formats import (FORMATS, FormatError, estimated_bytes, extension,
                             media_type, parse_format, write_collection)
from geoclip.geometry import clip_area

LOGGER = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = 'application/problem+json'

#: media types a client can ask for with Accept, most specific first
NEGOTIABLE = [(media_type(fmt), fmt) for fmt in FORMATS]

#: what the OpenAPI document says /clip can return, so that a browser
#: console offers a download instead of printing bytes as text
CLIP_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    200: {
        'description': 'The clipped features',
        'content': {
            'application/geo+json': {
                'schema': {'type': 'object'}
            },
            'application/geopackage+sqlite3': {
                'schema': {'type': 'string', 'format': 'binary'}
            },
            'application/flatgeobuf': {
                'schema': {'type': 'string', 'format': 'binary'}
            }
        }
    },
    400: {'model': Problem, 'description': 'Invalid request'},
    404: {'model': Problem, 'description': 'No such table'},
    413: {'model': Problem, 'description': 'Order larger than the limit'},
    503: {'model': Problem, 'description': 'Database unavailable'}
}

TABLE_DESCRIPTION = 'Table to clip, as "table" or "schema.table".'

BBOX_DESCRIPTION = ('Area of interest as minx,miny,maxx,maxy in the srid '
                    'CRS. Give this or wkt.')

WKT_DESCRIPTION = ('POLYGON or MULTIPOLYGON as WKT, for areas that fit in a '
                   'URL. Give this or bbox; post a GeoJSON geometry for '
                   'anything larger.')

DESCRIPTION = """
Clip data held in PostGIS to an area and download it as GeoJSON,
GeoPackage or FlatGeobuf.

The area can be WKT, a GeoJSON geometry/Feature/FeatureCollection, or a
bounding box, so a Leaflet, OpenLayers or MapLibre client can post what it
already has. `POST /estimate` sizes an order without building it.
"""


def _bbox(value: Optional[str]) -> Optional[List[float]]:
    """
    parse a bbox query parameter

    :param value: ``minx,miny,maxx,maxy``, or ``None``

    :returns: `list` of four floats, or ``None``

    :raises: `geoclip.errors.InvalidInputError`
    """

    if not value:
        return None

    parts = [part.strip() for part in value.split(',') if part.strip()]

    if len(parts) != 4:
        raise InvalidInputError(
            f'bbox must be minx,miny,maxx,maxy, got {value!r}')

    try:
        return [float(part) for part in parts]
    except ValueError:
        raise InvalidInputError(f'bbox must be four numbers, got {value!r}')


def _properties(value: Optional[str]) -> Optional[List[str]]:
    """
    parse a comma separated list of column names

    :param value: comma separated column names, or ``None``

    :returns: `list` of names, or ``None``
    """

    if not value:
        return None

    return [part.strip() for part in value.split(',') if part.strip()] or None


def build_database() -> ClipDatabase:
    """
    build the database layer from the environment

    :returns: geoclip.db.ClipDatabase
    """

    return ClipDatabase({})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """open the pool lazily and close it on shutdown"""

    app.state.database = build_database()
    LOGGER.info(f'clip service bound to {app.state.database.summary}')

    yield

    app.state.database.close()


def get_database(request: Request) -> ClipDatabase:
    """
    the database layer for this request

    :param request: the incoming request

    :returns: geoclip.db.ClipDatabase
    """

    return request.app.state.database


def problem(status: int, title: str, detail: Optional[str] = None,
            **extra: Any) -> JSONResponse:
    """
    build an RFC 9457 problem response

    :param status: HTTP status code
    :param title: short, stable summary
    :param detail: what went wrong this time
    :param extra: additional members

    :returns: `fastapi.responses.JSONResponse`
    """

    body = {'type': 'about:blank', 'title': title, 'status': status}

    if detail:
        body['detail'] = detail

    body.update(extra)

    return JSONResponse(body, status_code=status,
                        media_type=PROBLEM_MEDIA_TYPE)


def negotiate(requested: Optional[str], accept: Optional[str]) -> str:
    """
    decide the output format

    The `format` field wins; otherwise the Accept header is honoured, and
    GeoJSON is the default.

    :param requested: the format field of the request body
    :param accept: the Accept header

    :returns: `str` format key
    """

    if requested:
        return parse_format(requested)

    for media, fmt in NEGOTIABLE:
        if media in (accept or ''):
            return fmt

    return parse_format(None)


def download_name(table: Dict[str, Any], fmt: str) -> str:
    """
    name a download after its table and the time it was made

    :param table: table description
    :param fmt: format key

    :returns: `str` filename
    """

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stem = table['table'].strip('_') or 'clip'

    return f'{stem}_{stamp}.{extension(fmt)}'


def create_app() -> FastAPI:
    """
    build the FastAPI application

    :returns: `fastapi.FastAPI`
    """

    app = FastAPI(
        title='Geo clip download API',
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        contact={'name': 'KoalaGeo',
                 'url': 'https://github.com/KoalaGeo/Geo-clip-api'},
        license_info={'name': 'MIT'},
        # replaced below, so that the console's assets can be served from
        # somewhere reachable in an air-gapped cluster
        docs_url=None)

    swagger_js = os.environ.get(
        'GEOCLIP_SWAGGER_JS_URL',
        'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js')
    swagger_css = os.environ.get(
        'GEOCLIP_SWAGGER_CSS_URL',
        'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css')

    @app.get('/docs', include_in_schema=False)
    async def docs():
        # FastAPI's default console loads its JavaScript from a CDN, which
        # a cluster without egress cannot reach: the page renders empty.
        # These two variables point it at an internal copy instead.
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f'{app.title} - Swagger UI',
            swagger_js_url=swagger_js,
            swagger_css_url=swagger_css)

    origins = [o.strip() for o
               in os.environ.get('GEOCLIP_CORS_ORIGINS', '*').split(',')
               if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_methods=['GET', 'POST', 'OPTIONS'],
                       allow_headers=['*'],
                       expose_headers=['Content-Disposition',
                                       'X-Geoclip-Rows',
                                       'X-Geoclip-Truncated'])

    @app.exception_handler(TableNotFoundError)
    async def _table_not_found(request: Request, err: TableNotFoundError):
        return problem(HTTPStatus.NOT_FOUND, 'No such table', str(err))

    @app.exception_handler(InvalidInputError)
    async def _invalid_input(request: Request, err: InvalidInputError):
        return problem(HTTPStatus.BAD_REQUEST, 'Invalid request', str(err))

    @app.exception_handler(DatabaseError)
    async def _database_error(request: Request, err: DatabaseError):
        LOGGER.error(f'database error: {err}')
        return problem(HTTPStatus.SERVICE_UNAVAILABLE,
                       'Database unavailable', str(err))

    @app.exception_handler(FormatError)
    async def _format_error(request: Request, err: FormatError):
        LOGGER.error(f'format error: {err}')
        return problem(HTTPStatus.NOT_IMPLEMENTED, 'Format unavailable',
                       str(err))

    @app.get('/', summary='Service description')
    async def root() -> Dict[str, Any]:
        return {
            'title': 'Geo clip download API',
            'version': __version__,
            'formats': sorted(FORMATS),
            'links': {
                'tables': '/tables',
                'estimate': '/estimate',
                'clip': '/clip',
                'openapi': '/openapi.json',
                'docs': '/docs'
            }
        }

    @app.get('/healthz', summary='Liveness: the process is up')
    async def healthz() -> Dict[str, str]:
        # deliberately does not touch PostgreSQL: a database blip should
        # not have Kubernetes restart every pod
        return {'status': 'ok'}

    @app.get('/readyz', summary='Readiness: the database answers',
             responses={503: {'model': Problem}})
    async def readyz(database: ClipDatabase = Depends(get_database)):
        try:
            with database.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT 1')
        except GeoClipError as err:
            return problem(HTTPStatus.SERVICE_UNAVAILABLE,
                           'Database unavailable', str(err))

        return {'status': 'ready', 'database': database.summary}

    @app.get('/tables', response_model=TableList,
             summary='Tables available to clip')
    async def tables(schema: Optional[str] = None,
                     match: Optional[str] = None,
                     database: ClipDatabase = Depends(get_database)):
        found = database.list_tables(schema=schema, match=match)

        return TableList(
            tables=[TableInfo(**{**row, 'schema': row['schema']})
                    for row in found],
            count=len(found),
            schemas=database.allowed_schemas)

    @app.get('/tables/{name}', response_model=TableInfo,
             responses={404: {'model': Problem}},
             summary='One table')
    async def table(name: str, geometry_column: Optional[str] = None,
                    database: ClipDatabase = Depends(get_database)):
        found = database.get_table(name, geometry_column=geometry_column)

        return TableInfo(**{**found, 'schema': found['schema']})

    def _estimate(request: EstimateRequest, database: ClipDatabase):
        wkt, srid = clip_area(wkt=request.wkt, geometry=request.geometry,
                              bbox=request.bbox, srid=request.srid)
        table = database.get_table(
            request.table, geometry_column=request.geometry_column)
        fmt = parse_format(request.format)

        sized = database.estimate(table, wkt, wkt_srid=srid,
                                  exact_area=request.exact_area)
        limit = database.clamp_limit(request.limit)

        return EstimateResponse(
            table=table['name'],
            format=fmt,
            estimated_bytes=estimated_bytes(fmt, sized['geojson_bytes']),
            limit=limit,
            within_limit=sized['rows'] <= limit,
            **{k: sized[k] for k in ('rows', 'vertices',
                                     'requested_area_km2',
                                     'covered_area_km2')})

    def _clip(request: ClipRequest, accept: Optional[str],
              database: ClipDatabase):
        wkt, srid = clip_area(wkt=request.wkt, geometry=request.geometry,
                              bbox=request.bbox, srid=request.srid)
        fmt = negotiate(request.format, accept)
        simplify = parse_simplify(request.simplify)
        table = database.get_table(
            request.table, geometry_column=request.geometry_column)
        limit = database.clamp_limit(request.limit)

        if request.on_limit == 'error':
            # counting first costs a fraction of the clip, and means a paid
            # download is never quietly short of what was ordered
            rows = database.count_features(table, wkt, wkt_srid=srid)
            if rows > limit:
                return problem(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    'Order larger than the limit',
                    f'{rows} features exceeds the limit of {limit}; clip a '
                    'smaller area, raise the limit, or ask for '
                    'on_limit=truncate',
                    rows=rows, limit=limit)

        collection = database.clip(
            table, wkt, wkt_srid=srid, output_srid=request.output_srid,
            limit=limit, properties=request.properties,
            clip_geometries=request.clip, simplify=simplify)

        headers = {
            'X-Geoclip-Rows': str(collection['numberReturned']),
            'X-Geoclip-Truncated': str(collection['truncated']).lower()
        }

        if fmt == 'geojson':
            return JSONResponse(collection, headers=headers,
                                media_type=media_type(fmt))

        written_srid = None
        if table['srid']:
            written_srid = (request.output_srid
                            or database.default_output_srid)

        data = write_collection(collection, fmt, written_srid, table['name'])

        return _file_response(data, fmt, table, headers)

    # ---------------------------------------------------------- routes

    @app.post('/estimate', response_model=EstimateResponse,
              responses={400: {'model': Problem}, 404: {'model': Problem}},
              summary='Size an order without building it')
    async def estimate_post(request: EstimateRequest,
                            database: ClipDatabase = Depends(get_database)):
        return _estimate(request, database)

    @app.get('/estimate', response_model=EstimateResponse,
             responses={400: {'model': Problem}, 404: {'model': Problem}},
             summary='Size an order from query parameters')
    async def estimate_get(
            table: str = Query(description=TABLE_DESCRIPTION,
                               examples=['public.boreholes']),
            bbox: Optional[str] = Query(
                default=None, description=BBOX_DESCRIPTION,
                examples=['-3.30,55.90,-3.05,56.02']),
            wkt: Optional[str] = Query(default=None,
                                       description=WKT_DESCRIPTION),
            srid: int = Query(default=4326, gt=0,
                              description='EPSG code of the clip area.'),
            geometry_column: Optional[str] = Query(default=None),
            format: OutputFormat = Query(
                default=OutputFormat.geojson,
                description='Format to size the download in.'),
            exact_area: bool = Query(
                default=False,
                description='Union the clipped geometries instead of '
                            'summing their areas.'),
            limit: Optional[int] = Query(default=None, gt=0),
            database: ClipDatabase = Depends(get_database)):
        return _estimate(
            EstimateRequest(table=table, bbox=_bbox(bbox), wkt=wkt,
                            srid=srid, geometry_column=geometry_column,
                            format=format.value, exact_area=exact_area,
                            limit=limit),
            database)

    # response_class keeps FastAPI from adding a default application/json
    # entry next to the media types this route really returns
    @app.post('/clip', responses=CLIP_RESPONSES,
              summary='Clip a table and download it', response_model=None,
              response_class=Response)
    async def clip_post(request: ClipRequest, http_request: Request,
                        database: ClipDatabase = Depends(get_database)):
        return _clip(request, http_request.headers.get('accept'), database)

    @app.get('/clip', responses=CLIP_RESPONSES, response_model=None,
             response_class=Response,
             summary='Clip a table from query parameters, as a link')
    async def clip_get(
            http_request: Request,
            table: str = Query(description=TABLE_DESCRIPTION,
                               examples=['public.boreholes']),
            bbox: Optional[str] = Query(
                default=None, description=BBOX_DESCRIPTION,
                examples=['-3.30,55.90,-3.05,56.02']),
            wkt: Optional[str] = Query(default=None,
                                       description=WKT_DESCRIPTION),
            srid: int = Query(default=4326, gt=0,
                              description='EPSG code of the clip area.'),
            output_srid: Optional[int] = Query(
                default=None, gt=0,
                description='EPSG code of the returned geometries.'),
            geometry_column: Optional[str] = Query(default=None),
            properties: Optional[str] = Query(
                default=None,
                description='Comma separated columns to return.'),
            limit: Optional[int] = Query(default=None, gt=0),
            clip: bool = Query(
                default=True,
                description='False returns intersecting features whole.'),
            simplify: Optional[str] = Query(
                default=None,
                description='true, false, or a tolerance in output CRS '
                            'units.'),
            format: Optional[OutputFormat] = Query(
                default=None,
                description='Overrides the Accept header.'),
            on_limit: OnLimit = Query(
                default=OnLimit.error,
                description='error refuses an order larger than the limit.'),
            database: ClipDatabase = Depends(get_database)):
        return _clip(
            ClipRequest(
                table=table, bbox=_bbox(bbox), wkt=wkt, srid=srid,
                output_srid=output_srid, geometry_column=geometry_column,
                properties=_properties(properties), limit=limit, clip=clip,
                simplify=simplify,
                format=format.value if format else None,
                on_limit=on_limit.value),
            http_request.headers.get('accept'), database)

    return app


def _file_response(data: bytes, fmt: str, table: Dict[str, Any],
                   headers: Dict[str, str]) -> Response:
    """
    stream bytes from disk with a filename on them

    :param data: the written file
    :param fmt: format key
    :param table: table description
    :param headers: extra response headers

    :returns: `fastapi.Response`
    """

    handle = tempfile.NamedTemporaryFile(
        prefix='geoclip-', suffix=f'.{extension(fmt)}', delete=False)

    try:
        handle.write(data)
    finally:
        handle.close()

    path = Path(handle.name)

    return FileResponse(
        path, media_type=media_type(fmt),
        filename=download_name(table, fmt), headers=headers,
        background=BackgroundTask(path.unlink, missing_ok=True))


#: the ASGI application, for `uvicorn geoclip.api:app`
app = create_app()
