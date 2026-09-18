# =================================================================
#
# Geo clip API: test doubles
#
# =================================================================

"""A fake cursor so the SQL-building code can be tested without a server."""

from contextlib import contextmanager


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
