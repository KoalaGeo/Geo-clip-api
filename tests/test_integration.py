# =================================================================
#
# Geo clip API: integration tests
#
# =================================================================

"""End-to-end tests against a real PostGIS database.

Skipped unless ``GEOCLIP_TEST_DSN`` is set, e.g.::

    docker compose up -d postgres
    GEOCLIP_TEST_DSN="postgresql://postgres:postgres@localhost:5432/geodata" \\
        python3 -m pytest tests/test_integration.py

The demo tables loaded by ``docker/initdb/01-demo-data.sql`` are assumed.
"""

import os

import pytest

from geoclip.db import ClipDatabase
from geoclip.errors import TableNotFoundError

DSN = os.environ.get('GEOCLIP_TEST_DSN')

pytestmark = pytest.mark.skipif(
    not DSN, reason='GEOCLIP_TEST_DSN is not set')

# covers the two demo boreholes near Edinburgh, not the third
EDINBURGH = ('POLYGON((-3.25 55.92, -3.10 55.92, -3.10 56.00, '
             '-3.25 56.00, -3.25 55.92))')


@pytest.fixture(scope='module')
def db():
    database = ClipDatabase({'dsn': DSN})
    yield database
    database.close()


def test_demo_tables_are_listed(db):
    names = [t['name'] for t in db.list_tables()]

    assert 'public.boreholes' in names
    assert 'public.bedrock' in names


def test_match_filter(db):
    assert [t['name'] for t in db.list_tables(match='bedrock')] == \
        ['public.bedrock']


def test_clip_points(db):
    table = db.get_table('boreholes')

    collection = db.clip(table, EDINBURGH)

    assert collection['numberReturned'] == 2
    assert {f['properties']['name'] for f in collection['features']} == \
        {'BH001', 'BH002'}
    assert 'geom' not in collection['features'][0]['properties']


def test_clip_reprojects_from_another_crs(db):
    table = db.get_table('bedrock')

    # clip geometry in WGS84, table in British National Grid
    collection = db.clip(table, EDINBURGH)

    assert collection['numberReturned'] == 1
    feature = collection['features'][0]
    assert feature['geometry']['type'] in ('Polygon', 'MultiPolygon')

    # clipped to the area of interest, so no coordinate escapes it
    coordinates = str(feature['geometry']['coordinates'])
    assert '-3.2' in coordinates or '-3.1' in coordinates


def test_clip_can_return_whole_features(db):
    table = db.get_table('bedrock')

    clipped = db.clip(table, EDINBURGH)
    whole = db.clip(table, EDINBURGH, clip_geometries=False)

    assert (str(whole['features'][0]['geometry']) !=
            str(clipped['features'][0]['geometry']))


def test_property_subset(db):
    table = db.get_table('boreholes')

    collection = db.clip(table, EDINBURGH, properties=['name'])

    assert list(collection['features'][0]['properties']) == ['name']


def test_limit_is_applied(db):
    table = db.get_table('boreholes')

    collection = db.clip(table, EDINBURGH, limit=1)

    assert collection['numberReturned'] == 1
    assert collection['truncated'] is True


def test_output_reprojection(db):
    table = db.get_table('boreholes')

    collection = db.clip(table, EDINBURGH, output_srid=27700)

    x, y = collection['features'][0]['geometry']['coordinates']
    assert 300000 < x < 340000
    assert 660000 < y < 690000


def test_unknown_table_is_rejected(db):
    with pytest.raises(TableNotFoundError):
        db.get_table('pg_class')


def test_empty_result_is_still_geojson(db):
    table = db.get_table('boreholes')

    collection = db.clip(
        table, 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))')

    assert collection == {
        'type': 'FeatureCollection',
        'features': [],
        'numberReturned': 0,
        'truncated': False
    }
