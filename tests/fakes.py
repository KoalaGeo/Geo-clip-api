# =================================================================
#
# Geo clip API: test doubles
#
# =================================================================

"""A fake cursor so the SQL-building code can be tested without a server."""

from contextlib import contextmanager

from geoclip.errors import DatabaseError, TableNotFoundError


class FakeCursor:
    """records executed statements and replays canned rows"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def execute(self, query, parameters=None):
        self.calls.append((query, parameters))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    @property
    def last_parameters(self):
        return self.calls[-1][1]


def patch_cursor(db, rows=None):
    """
    replace a `ClipDatabase`'s cursor with a `FakeCursor`

    :param db: `geoclip.db.ClipDatabase`
    :param rows: rows the cursor should return

    :returns: the `FakeCursor`
    """

    cursor = FakeCursor(rows)

    @contextmanager
    def _cursor():
        yield cursor

    db._cursor = _cursor

    return cursor


BOREHOLES_ROW = {
    'schema': 'public',
    'table': 'boreholes',
    'geometry_column': 'geom',
    'geometry_type': 'POINT',
    'srid': 4326,
    'coordinate_dimension': 2,
    'description': 'Demo borehole locations',
    'estimated_rows': 3
}

BEDROCK_ROW = {
    'schema': 'public',
    'table': 'bedrock',
    'geometry_column': 'geom',
    'geometry_type': 'MULTIPOLYGON',
    'srid': 27700,
    'coordinate_dimension': 2,
    'description': None,
    'estimated_rows': 2
}


class FakeDatabase:
    """a `ClipDatabase` without a database, for the HTTP API tests"""

    summary = 'fake'
    allowed_schemas = ['public']
    default_output_srid = 4326

    def __init__(self, tables=None, rows=2, error=None, ready=True):
        self.tables = tables if tables is not None else [BOREHOLES_ROW,
                                                         BEDROCK_ROW]
        self.rows = rows
        self.error = error
        self.ready = ready
        self.calls = []

    # ------------------------------------------------------------ helpers

    def _named(self, row):
        return dict(row, name=f"{row['schema']}.{row['table']}")

    def _raise(self):
        if self.error:
            raise self.error

    # ---------------------------------------------------------- interface

    def clamp_limit(self, limit):
        return int(limit) if limit else 1000

    def list_tables(self, schema=None, match=None):
        self._raise()
        self.calls.append(('list_tables', schema, match))
        return [self._named(t) for t in self.tables]

    def get_table(self, name, geometry_column=None):
        self._raise()
        for table in self.tables:
            if name in (table['table'], f"{table['schema']}.{table['table']}"):
                return self._named(table)
        raise TableNotFoundError(f'table {name!r} is not available')

    def count_features(self, table, wkt, wkt_srid=4326):
        self._raise()
        self.calls.append(('count_features', table['name']))
        return self.rows

    def estimate(self, table, wkt, wkt_srid=4326, exact_area=False):
        self._raise()
        self.calls.append(('estimate', table['name'], exact_area))
        return {
            'rows': self.rows,
            'vertices': self.rows * 40,
            'geojson_bytes': self.rows * 2000,
            'requested_area_km2': 208.62,
            'covered_area_km2': 123.94
        }

    def clip(self, table, wkt, limit=None, **kwargs):
        self._raise()
        self.calls.append(('clip', table['name'], kwargs))
        returned = min(self.rows, self.clamp_limit(limit))
        return {
            'type': 'FeatureCollection',
            'features': [{
                'type': 'Feature',
                'geometry': {'type': 'Point', 'coordinates': [-3.19, 55.95]},
                'properties': {'id': n, 'name': f'BH{n:03}'}
            } for n in range(1, returned + 1)],
            'numberReturned': returned,
            'truncated': returned < self.rows
        }

    @contextmanager
    def connection(self):
        if not self.ready:
            raise DatabaseError('could not connect to the database: nope')
        yield FakeConnection()

    def close(self):
        pass


class FakeConnection:
    """just enough connection for the readiness probe"""

    @contextmanager
    def cursor(self, *args, **kwargs):
        yield FakeCursor()
