# =================================================================
#
# Geo clip API: PostGIS access layer
#
# =================================================================

"""Thin PostGIS access layer used by the pygeoapi process plugins.

Two operations are exposed:

* :meth:`ClipDatabase.list_tables` - which spatial tables can be clipped
* :meth:`ClipDatabase.clip` - clip one of those tables to a WKT geometry
  and return a GeoJSON ``FeatureCollection``

Every identifier that reaches SQL is first looked up in
``geometry_columns`` and then composed with :mod:`psycopg2.sql`, so a table
name coming from a request body can never be injected into a statement.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from geoclip.errors import DatabaseError, InvalidInputError, TableNotFoundError

LOGGER = logging.getLogger(__name__)

#: identifiers we are prepared to quote and hand to PostgreSQL.
#: a leading digit is allowed because real data has it (an OGR layer named
#: "625k_V5_BEDROCK_Geology" lands as a table of the same name); such names
#: are legal in PostgreSQL when quoted, which psycopg2.sql.Identifier always
#: does. Safety comes from this character set plus the geometry_columns
#: lookup, not from the first character.
IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_$]{0,62}$')

#: never exposed, whatever the configuration asks for
FORBIDDEN_SCHEMAS = frozenset([
    'information_schema', 'pg_catalog', 'pg_toast', 'topology', 'tiger',
    'tiger_data'
])

DEFAULT_ALLOWED_SCHEMAS = ['public']
DEFAULT_LIMIT = 1000
DEFAULT_MAX_FEATURES = 10000
DEFAULT_OUTPUT_SRID = 4326
DEFAULT_STATEMENT_TIMEOUT = 60000  # ms
DEFAULT_POOL_MAX = 5
DEFAULT_COORDINATE_PRECISION = 7
#: automatic simplification uses the clip extent divided by this, i.e. about
#: one pixel on a 2000 pixel wide map
DEFAULT_SIMPLIFY_DIVISOR = 2000

#: bytes per coordinate pair in GeoJSON at 7 decimal places, measured
BYTES_PER_VERTEX = 24

#: vertices the clip boundary adds to each cut feature
VERTICES_PER_CUT = 5

LIST_TABLES_SQL = """
SELECT g.f_table_schema AS schema,
       g.f_table_name AS "table",
       g.f_geometry_column AS geometry_column,
       g.type AS geometry_type,
       g.srid AS srid,
       g.coord_dimension AS coordinate_dimension,
       obj_description(c.oid, 'pg_class') AS description,
       CASE WHEN c.reltuples < 0 THEN NULL
            ELSE c.reltuples::bigint END AS estimated_rows
FROM geometry_columns g
JOIN pg_namespace n ON n.nspname = g.f_table_schema
JOIN pg_class c ON c.relname = g.f_table_name AND c.relnamespace = n.oid
WHERE g.f_table_schema = ANY(%(schemas)s)
ORDER BY g.f_table_schema, g.f_table_name, g.f_geometry_column
"""

COLUMNS_SQL = """
SELECT attname AS column
FROM pg_attribute
WHERE attrelid = format('%%I.%%I', %(schema)s, %(table)s)::regclass
  AND attnum > 0
  AND NOT attisdropped
"""


def _first_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """return the first non-empty environment variable of ``names``"""

    for name in names:
        value = os.environ.get(name)
        if value:
            return value

    return default


def _connection_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    build psycopg2 connection settings from plugin config and environment

    :param config: the `data` block of the processor definition

    :returns: `dict` of keyword arguments for `psycopg2.connect`
    """

    dsn = config.get('dsn') or _first_env('GEOCLIP_DSN', 'DATABASE_URL')
    if dsn:
        return {'dsn': dsn, 'application_name': 'geo-clip-api'}

    return {
        'host': config.get('host') or _first_env(
            'POSTGRES_HOST', 'PGHOST', default='localhost'),
        'port': int(config.get('port') or _first_env(
            'POSTGRES_PORT', 'PGPORT', default='5432')),
        'dbname': (config.get('dbname') or config.get('database')
                   or _first_env('POSTGRES_DB', 'PGDATABASE',
                                 default='postgres')),
        'user': config.get('user') or _first_env(
            'POSTGRES_USER', 'PGUSER', default='postgres'),
        'password': config.get('password') or _first_env(
            'POSTGRES_PASSWORD', 'PGPASSWORD', default=''),
        'application_name': 'geo-clip-api'
    }


def _km2(value: Any) -> Optional[float]:
    """
    convert an area in square metres to square kilometres

    :param value: area in m2, or ``None``

    :returns: `float` km2 rounded to 2 places, or ``None``
    """

    if value is None:
        return None

    return round(float(value) / 1e6, 2)


def table_label(table: Dict[str, Any]) -> str:
    """
    qualified name of a table description, however it was built

    :param table: table description

    :returns: `str` ``schema.table``
    """

    return table.get('name') or f"{table['schema']}.{table['table']}"


def as_list(value: Any, kind: str) -> List[str]:
    """
    read a list setting that may have come from an environment variable

    pygeoapi expands ``${VAR}`` in its configuration to a scalar, so a
    setting such as `allowed_tables` cannot be written as a YAML list when
    it is driven by the environment. A comma separated string is accepted
    for exactly that case.

    :param value: YAML list, comma separated string, or ``None``
    :param kind: setting name, for error messages

    :returns: `list` of `str`
    """

    if value is None or value == '':
        return []

    if isinstance(value, str):
        return [item.strip() for item in value.split(',') if item.strip()]

    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]

    raise InvalidInputError(
        f'{kind} must be a list or a comma separated string, got {value!r}')


def as_bool(value: Any, kind: str, default: bool = False) -> bool:
    """
    read a boolean setting that may have come from an environment variable

    :param value: `bool`, string, or ``None``
    :param kind: setting name, for error messages
    :param default: value to use when the setting is absent

    :returns: `bool`
    """

    if value is None or value == '':
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        if value.strip().lower() in ('true', 't', 'yes', 'y', '1'):
            return True
        if value.strip().lower() in ('false', 'f', 'no', 'n', '0'):
            return False

    raise InvalidInputError(f'{kind} must be a boolean, got {value!r}')


def parse_simplify(simplify: Any) -> Any:
    """
    validate the simplify input

    :param simplify: ``True``/``False``/``None``, or a tolerance in
                     output CRS units

    :returns: ``True``, ``None``, or a `float` tolerance
    """

    if simplify in (None, False, ''):
        return None

    if simplify is True:
        return True

    if isinstance(simplify, str):
        lowered = simplify.strip().lower()
        if lowered in ('true', 't', 'yes', 'y'):
            return True
        if lowered in ('false', 'f', 'no', 'n'):
            return None

    try:
        tolerance = float(simplify)
    except (TypeError, ValueError):
        raise InvalidInputError(
            'simplify must be true, false, or a tolerance in output CRS '
            f'units, got {simplify!r}')

    if tolerance <= 0:
        raise InvalidInputError(
            f'simplify tolerance must be greater than zero, got '
            f'{tolerance}')

    return tolerance


def validate_identifier(value: str, kind: str = 'identifier') -> str:
    """
    check that a single SQL identifier is safe to quote

    :param value: candidate identifier
    :param kind: what the identifier names (used in the error message)

    :returns: the identifier

    :raises: `geoclip.errors.InvalidInputError`
    """

    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise InvalidInputError(
            f'invalid {kind}: {value!r} (expected an unquoted PostgreSQL '
            'identifier, e.g. "roads" or "os_open_roads")')

    return value


def split_table_name(name: str) -> Tuple[Optional[str], str]:
    """
    split a ``schema.table`` (or ``table``) reference

    :param name: table reference

    :returns: `tuple` of (schema or ``None``, table)

    :raises: `geoclip.errors.InvalidInputError`
    """

    if not isinstance(name, str) or not name.strip():
        raise InvalidInputError('a table name is required')

    parts = name.strip().split('.')

    if len(parts) == 1:
        return None, validate_identifier(parts[0], 'table name')
    elif len(parts) == 2:
        return (validate_identifier(parts[0], 'schema name'),
                validate_identifier(parts[1], 'table name'))

    raise InvalidInputError(
        f'invalid table name: {name!r} (expected "table" or "schema.table")')


class ClipDatabase:
    """PostGIS access for the clip and list-tables processes"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initialize object

        :param config: `dict` of plugin configuration (the `data` block of
                       the pygeoapi processor definition).  Connection
                       details fall back to the usual libpq/POSTGRES_*
                       environment variables.

        :returns: geoclip.db.ClipDatabase
        """

        config = dict(config or {})

        self._settings = _connection_settings(config)
        self._pool = None
        self._pool_lock = threading.Lock()
        self.pool_min = int(config.get('pool_min', 1))
        self.pool_max = int(config.get('pool_max', DEFAULT_POOL_MAX))

        schemas = (as_list(config.get('allowed_schemas'), 'allowed_schemas')
                   or DEFAULT_ALLOWED_SCHEMAS)
        self.allowed_schemas = [
            validate_identifier(s, 'schema name') for s in schemas
            if s not in FORBIDDEN_SCHEMAS
        ]
        if not self.allowed_schemas:
            raise InvalidInputError(
                'allowed_schemas resolved to an empty list; nothing could '
                'ever be published')

        self.allowed_tables = frozenset(
            as_list(config.get('allowed_tables'), 'allowed_tables'))
        self.excluded_tables = frozenset(
            as_list(config.get('excluded_tables'), 'excluded_tables'))

        self.default_limit = int(config.get('default_limit', DEFAULT_LIMIT))
        self.max_features = int(config.get('max_features',
                                           DEFAULT_MAX_FEATURES))
        self.default_output_srid = int(config.get('output_srid',
                                                  DEFAULT_OUTPUT_SRID))
        self.statement_timeout = int(config.get('statement_timeout',
                                                DEFAULT_STATEMENT_TIMEOUT))
        self.coordinate_precision = int(config.get(
            'coordinate_precision', DEFAULT_COORDINATE_PRECISION))
        self.simplify_divisor = float(config.get('simplify_divisor')
                                      or DEFAULT_SIMPLIFY_DIVISOR)
        #: run source geometries through ST_MakeValid before intersecting
        self.make_valid_source = as_bool(config.get('make_valid_source'),
                                         'make_valid_source')

    def __repr__(self):
        return f'<ClipDatabase> {self.summary}'

    @property
    def summary(self) -> str:
        """connection summary, safe to log (no password)"""

        if 'dsn' in self._settings:
            return 'dsn'

        return '{host}:{port}/{dbname}'.format(**self._settings)

    def clamp_limit(self, limit: Optional[int]) -> int:
        """
        apply the configured default and ceiling to a requested limit

        :param limit: requested number of features, or ``None``

        :returns: `int` limit to use
        """

        if limit in (None, ''):
            limit = self.default_limit

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise InvalidInputError(f'limit must be an integer, got {limit!r}')

        if limit < 1:
            raise InvalidInputError('limit must be greater than zero')

        return min(limit, self.max_features)

    def is_allowed(self, schema: str, table: str) -> bool:
        """
        check a table against the configured allow/deny lists

        :param schema: schema name
        :param table: table name

        :returns: `bool`
        """

        names = {f'{schema}.{table}', table}

        if self.allowed_tables and not (names & self.allowed_tables):
            return False

        return not (names & self.excluded_tables)

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------

    def _get_pool(self) -> ThreadedConnectionPool:
        """lazily create the connection pool"""

        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    LOGGER.debug(f'creating pool for {self.summary}')
                    settings = dict(self._settings)
                    dsn = settings.pop('dsn', None)
                    args = (dsn,) if dsn else ()
                    self._pool = ThreadedConnectionPool(
                        self.pool_min, self.pool_max, *args, **settings)

        return self._pool

    @contextmanager
    def connection(self):
        """
        yield a read-only connection from the pool

        :returns: `psycopg2` connection
        """

        try:
            pool = self._get_pool()
            conn = pool.getconn()
        except psycopg2.Error as err:
            LOGGER.error(f'could not connect to {self.summary}: {err}')
            raise DatabaseError(f'could not connect to the database: {err}')

        try:
            conn.set_session(readonly=True, autocommit=False)
            yield conn
            conn.rollback()
        except psycopg2.Error as err:
            conn.rollback()
            LOGGER.error(f'query failed: {err}')
            raise DatabaseError(str(err).strip())
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    @contextmanager
    def _cursor(self):
        """yield a dict cursor with the configured statement timeout"""

        with self.connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    sql.SQL('SET LOCAL statement_timeout = {}').format(
                        sql.Literal(self.statement_timeout)))
                yield cur

    def close(self) -> None:
        """close the connection pool, if one was opened"""

        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    # ------------------------------------------------------------------
    # catalogue
    # ------------------------------------------------------------------

    def list_tables(self, schema: Optional[str] = None,
                    match: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        list the spatial tables available for clipping

        :param schema: optional schema to restrict the listing to
        :param match: optional case-insensitive substring filter applied to
                      the qualified table name

        :returns: `list` of `dict` table descriptions
        """

        schemas = self.allowed_schemas

        if schema is not None:
            schema = validate_identifier(schema, 'schema name')
            if schema not in self.allowed_schemas:
                raise InvalidInputError(
                    f'schema {schema!r} is not published; available schemas: '
                    f'{", ".join(self.allowed_schemas)}')
            schemas = [schema]

        with self._cursor() as cur:
            cur.execute(LIST_TABLES_SQL, {'schemas': list(schemas)})
            rows = [dict(row) for row in cur.fetchall()]

        tables = []

        for row in rows:
            if not self.is_allowed(row['schema'], row['table']):
                continue

            row['name'] = f"{row['schema']}.{row['table']}"

            if match and match.lower() not in row['name'].lower():
                continue

            tables.append(row)

        return tables

    def get_table(self, name: str,
                  geometry_column: Optional[str] = None) -> Dict[str, Any]:
        """
        resolve and validate a user supplied table reference

        :param name: ``table`` or ``schema.table``
        :param geometry_column: geometry column, required only for tables
                                carrying more than one

        :returns: `dict` table description (as `list_tables` entries)

        :raises: `geoclip.errors.TableNotFoundError`
        """

        schema, table = split_table_name(name)

        if geometry_column is not None:
            geometry_column = validate_identifier(
                geometry_column, 'geometry column')

        candidates = [
            t for t in self.list_tables(schema=schema)
            if t['table'] == table
            and geometry_column in (None, t['geometry_column'])
        ]

        if not candidates:
            raise TableNotFoundError(
                f'table {name!r} is not available; list the published '
                'tables to see what can be clipped')

        schemas = {t['schema'] for t in candidates}
        if len(schemas) > 1:
            found = ', '.join(sorted(f'{s}.{table}' for s in schemas))
            raise InvalidInputError(
                f'table name {name!r} is ambiguous, it exists in several '
                f'schemas ({found}); qualify it as "schema.table"')

        if len(candidates) > 1:
            columns = ', '.join(sorted(t['geometry_column']
                                       for t in candidates))
            raise InvalidInputError(
                f'table {name!r} has more than one geometry column '
                f'({columns}); set the geometry_column input')

        return candidates[0]

    def table_columns(self, schema: str, table: str) -> List[str]:
        """
        list the column names of a table

        :param schema: schema name
        :param table: table name

        :returns: `list` of column names
        """

        with self._cursor() as cur:
            cur.execute(COLUMNS_SQL, {'schema': schema, 'table': table})
            return [row['column'] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # clipping
    # ------------------------------------------------------------------

    def _properties_expression(self, table: Dict[str, Any],
                               properties: Optional[Iterable[str]]):
        """
        build the jsonb expression producing each feature's properties

        :param table: table description from `get_table`
        :param properties: optional subset of columns to return

        :returns: `psycopg2.sql.Composable`
        """

        if not properties:
            # every column except the geometry, which travels separately
            return sql.SQL('to_jsonb(t) - {}').format(
                sql.Literal(table['geometry_column']))

        requested = [validate_identifier(p, 'property name')
                     for p in properties]
        available = set(self.table_columns(table['schema'], table['table']))
        unknown = [p for p in requested if p not in available]

        if unknown:
            raise InvalidInputError(
                f'unknown column(s) for {table_label(table)}: '
                f'{", ".join(unknown)}')

        pairs = [
            sql.SQL('{}, t.{}').format(sql.Literal(p), sql.Identifier(p))
            for p in requested if p != table['geometry_column']
        ]

        if not pairs:
            return sql.SQL("'{}'::jsonb")

        return sql.SQL('jsonb_build_object({})').format(
            sql.SQL(', ').join(pairs))

    def _build_clip_query(self, table: Dict[str, Any],
                          properties: Optional[Iterable[str]] = None,
                          clip_geometries: bool = True,
                          simplify: Any = None):
        """
        compose the clipping statement for a table

        :param table: table description from `get_table`
        :param properties: optional subset of columns to return
        :param clip_geometries: whether geometries are cut at the boundary
                                of the clip geometry (`True`) or returned
                                whole (`False`)
        :param simplify: ``True`` to simplify output geometries with a
                         tolerance derived from the clip extent, a number
                         for an explicit tolerance in output CRS units, or
                         ``None``/``False`` for no simplification

        :returns: `psycopg2.sql.Composable`
        """

        table_srid = table['srid'] or 0
        geom_col = sql.Identifier(table['geometry_column'])
        relation = sql.Identifier(table['schema'], table['table'])

        if table_srid:
            # bring the clip geometry into the table's CRS so the spatial
            # index on the table can be used
            clip_in_table_srid = sql.SQL(
                'ST_Transform(clip.geom, {})').format(sql.Literal(table_srid))
        else:
            # unknown CRS: assume the caller's geometry is already in it
            LOGGER.warning(
                f'{table_label(table)}.{table["geometry_column"]} has SRID 0; '
                'assuming the input geometry uses the same CRS')
            clip_in_table_srid = sql.SQL('ST_SetSRID(clip.geom, 0)')

        source_geom = sql.SQL('t.{}').format(geom_col)
        if self.make_valid_source:
            source_geom = sql.SQL('ST_MakeValid({})').format(source_geom)

        if clip_geometries:
            geom_expr = sql.SQL('ST_Intersection({}, target.geom)').format(
                source_geom)
        else:
            geom_expr = source_geom

        if table_srid:
            output_geom = sql.SQL('ST_Transform(geom, %(output_srid)s)')
            clip_in_output_srid = sql.SQL(
                'ST_Transform(clip.geom, %(output_srid)s)')
        else:
            output_geom = sql.SQL('geom')
            clip_in_output_srid = sql.SQL('clip.geom')

        simplify_cte = sql.SQL('')

        if simplify is True:
            # one tolerance for the whole response, scaled to the area
            # asked for: a viewport sized clip is simplified to about a
            # pixel, a field sized one barely at all
            simplify_cte = sql.SQL("""
), tolerance AS (
    SELECT GREATEST(ST_XMax(g) - ST_XMin(g), ST_YMax(g) - ST_YMin(g))
           / %(simplify_divisor)s AS value
    FROM (SELECT {clip_in_output_srid} AS g FROM clip) extent""").format(
                clip_in_output_srid=clip_in_output_srid)
            output_geom = sql.SQL(
                'ST_SimplifyPreserveTopology({}, (SELECT value FROM '
                'tolerance))').format(output_geom)
        elif simplify is not None and simplify is not False:
            output_geom = sql.SQL(
                'ST_SimplifyPreserveTopology({}, %(simplify_tolerance)s)'
            ).format(output_geom)

        return sql.SQL("""
WITH clip AS (
    SELECT ST_MakeValid(
               ST_SetSRID(ST_GeomFromText(%(wkt)s), %(wkt_srid)s)) AS geom
{simplify_cte}
), target AS (
    SELECT {clip_in_table_srid} AS geom FROM clip
), matched AS (
    SELECT {properties} AS props,
           {geom_expr} AS geom
    FROM {relation} AS t, target
    WHERE t.{geom_col} && target.geom
      AND ST_Intersects(t.{geom_col}, target.geom)
), kept AS (
    SELECT props, geom
    FROM matched
    WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom)
    LIMIT %(limit)s
)
SELECT jsonb_build_object(
    'type', 'FeatureCollection',
    'features', COALESCE(jsonb_agg(jsonb_build_object(
        'type', 'Feature',
        'geometry', ST_AsGeoJSON({output_geom}, %(precision)s)::jsonb,
        'properties', props)), '[]'::jsonb)
) AS collection
FROM kept
""").format(
            clip_in_table_srid=clip_in_table_srid,
            simplify_cte=simplify_cte,
            properties=self._properties_expression(table, properties),
            geom_expr=geom_expr,
            relation=relation,
            geom_col=geom_col,
            output_geom=output_geom)

    def count_features(self, table: Dict[str, Any], wkt: str,
                       wkt_srid: int = 4326) -> int:
        """
        count the features a clip would return

        The spatial predicate only: no intersection, no JSON building. This
        is what makes a limit check affordable before a download rather
        than after it.

        :param table: table description from `get_table`
        :param wkt: clip geometry as WKT
        :param wkt_srid: SRID of the clip geometry

        :returns: `int` number of features
        """

        table_srid = table['srid'] or 0
        geom_col = sql.Identifier(table['geometry_column'])
        relation = sql.Identifier(table['schema'], table['table'])

        if table_srid:
            clip_in_table_srid = sql.SQL(
                'ST_Transform(clip.geom, {})').format(sql.Literal(table_srid))
        else:
            clip_in_table_srid = sql.SQL('ST_SetSRID(clip.geom, 0)')

        query = sql.SQL("""
WITH clip AS (
    SELECT ST_MakeValid(
               ST_SetSRID(ST_GeomFromText(%(wkt)s), %(wkt_srid)s)) AS geom
), target AS (
    SELECT {clip_in_table_srid} AS geom FROM clip
)
SELECT count(*) AS rows
FROM {relation} AS t, target
WHERE t.{geom_col} && target.geom
  AND ST_Intersects(t.{geom_col}, target.geom)
""").format(clip_in_table_srid=clip_in_table_srid,
            geom_col=geom_col, relation=relation)

        with self._cursor() as cur:
            cur.execute(query, {'wkt': wkt, 'wkt_srid': int(wkt_srid)})
            row = cur.fetchone() or {}

        return int(row.get('rows') or 0)

    def estimate(self, table: Dict[str, Any], wkt: str, wkt_srid: int = 4326,
                 exact_area: bool = False) -> Dict[str, Any]:
        """
        size an order without building it

        Everything here is one pass over the rows the clip would touch: the
        row count, the area that actually has data in it, and enough to
        model the response size. It costs a fraction of the clip itself,
        which is what makes quoting affordable.

        :param table: table description from `get_table`
        :param wkt: clip geometry as WKT
        :param wkt_srid: SRID of the clip geometry
        :param exact_area: union the clipped geometries rather than summing
                           their areas; slower, and only differs when the
                           source features overlap each other

        :returns: `dict` with rows, areas in km2 and an estimated size
        """

        table_srid = table['srid'] or 0
        geom_col = sql.Identifier(table['geometry_column'])
        relation = sql.Identifier(table['schema'], table['table'])

        if table_srid:
            clip_in_table_srid = sql.SQL(
                'ST_Transform(clip.geom, {})').format(sql.Literal(table_srid))
        else:
            clip_in_table_srid = sql.SQL('ST_SetSRID(clip.geom, 0)')

        # summing the clipped areas double counts where source features
        # overlap each other; the union is exact and about three times
        # slower, so it is opt-in
        covered_m2 = sql.SQL(
            'COALESCE(sum(ST_Area(ST_Transform(cut, 4326)::geography)), 0)')
        if exact_area:
            covered_m2 = sql.SQL(
                'COALESCE(ST_Area('
                'ST_Transform(ST_Union(cut), 4326)::geography), 0)')

        query = sql.SQL("""
WITH clip AS (
    SELECT ST_MakeValid(
               ST_SetSRID(ST_GeomFromText(%(wkt)s), %(wkt_srid)s)) AS geom
), target AS (
    SELECT {clip_in_table_srid} AS geom FROM clip
), matched AS (
    SELECT t.{geom_col} AS source,
           ST_Intersection(t.{geom_col}, target.geom) AS cut,
           length((to_jsonb(t) - %(geom_name)s)::text) AS property_bytes
    FROM {relation} AS t, target
    WHERE t.{geom_col} && target.geom
      AND ST_Intersects(t.{geom_col}, target.geom)
)
SELECT count(*) AS rows,
       COALESCE(sum(ST_NPoints(source)), 0) AS source_vertices,
       COALESCE(sum(ST_Area(source)), 0) AS source_area,
       COALESCE(sum(ST_Area(cut)), 0) AS covered_area,
       COALESCE(avg(property_bytes), 0) AS property_bytes,
       {covered_m2} AS covered_area_m2,
       (SELECT ST_Area(ST_Transform(geom, 4326)::geography) FROM clip)
           AS requested_area_m2
FROM matched
""").format(clip_in_table_srid=clip_in_table_srid,
            geom_col=geom_col,
            relation=relation,
            covered_m2=covered_m2)

        parameters = {
            'wkt': wkt,
            'wkt_srid': int(wkt_srid),
            'geom_name': table['geometry_column']
        }

        with self._cursor() as cur:
            cur.execute(query, parameters)
            row = dict(cur.fetchone() or {})

        return self._size_estimate(row)

    @staticmethod
    def _size_estimate(row: Dict[str, Any]) -> Dict[str, Any]:
        """
        turn the estimate query's row into an order summary

        The size model is rows x property bytes + vertices x bytes per
        vertex, with the vertex count scaled by how much of each feature
        survives the clip. Measured against real clips of the 1:625k
        geology it lands within about 20%.

        :param row: row from the estimate query

        :returns: `dict` summary
        """

        rows = int(row.get('rows') or 0)
        vertices = float(row.get('source_vertices') or 0)
        source_area = float(row.get('source_area') or 0)
        covered_area = float(row.get('covered_area') or 0)
        property_bytes = float(row.get('property_bytes') or 0)

        retained = 1.0
        if source_area > 0:
            retained = min(covered_area / source_area, 1.0)

        output_vertices = vertices * retained + VERTICES_PER_CUT * rows
        geojson_bytes = int(rows * property_bytes
                            + BYTES_PER_VERTEX * output_vertices)

        return {
            'rows': rows,
            'vertices': int(output_vertices),
            'geojson_bytes': geojson_bytes if rows else 0,
            'requested_area_km2': _km2(row.get('requested_area_m2')),
            'covered_area_km2': _km2(row.get('covered_area_m2'))
        }

    def clip(self, table: Dict[str, Any], wkt: str, wkt_srid: int = 4326,
             output_srid: Optional[int] = None, limit: Optional[int] = None,
             properties: Optional[Iterable[str]] = None,
             clip_geometries: bool = True,
             simplify: Any = None) -> Dict[str, Any]:
        """
        clip a table to a WKT geometry

        :param table: table description from `get_table`
        :param wkt: clip geometry as WKT (polygon or multipolygon)
        :param wkt_srid: SRID of the clip geometry
        :param output_srid: SRID of the returned GeoJSON (default 4326)
        :param limit: maximum number of features to return
        :param properties: optional subset of columns to return
        :param clip_geometries: cut geometries at the clip boundary
        :param simplify: ``True`` to simplify output geometries to about a
                         pixel of the clip extent, or a tolerance in output
                         CRS units

        :returns: `dict` GeoJSON FeatureCollection
        """

        limit = self.clamp_limit(limit)
        output_srid = int(output_srid or self.default_output_srid)
        simplify = parse_simplify(simplify)
        query = self._build_clip_query(table, properties, clip_geometries,
                                       simplify)

        parameters = {
            'wkt': wkt,
            'wkt_srid': int(wkt_srid),
            'output_srid': output_srid,
            'limit': limit,
            'precision': self.coordinate_precision,
            'simplify_divisor': self.simplify_divisor,
            'simplify_tolerance': simplify if simplify is not True else None
        }

        with self._cursor() as cur:
            cur.execute(query, parameters)
            row = cur.fetchone()

        collection = (row or {}).get('collection') or {
            'type': 'FeatureCollection', 'features': []
        }

        features = collection.get('features', [])
        collection['numberReturned'] = len(features)
        collection['truncated'] = len(features) >= limit

        if table['srid'] and output_srid != 4326:
            # RFC 7946 GeoJSON is always WGS84; flag anything else explicitly
            collection['crs'] = {
                'type': 'name',
                'properties': {'name': f'urn:ogc:def:crs:EPSG::{output_srid}'}
            }

        return collection
