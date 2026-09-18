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

#: loaded from tests/625k_V5_Geology_UK_EPSG27700.gpkg by the compose
#: gpkg-loader stage; the tests using it skip when it is not there
GEOPACKAGE_TABLE = '625k_v5_bedrock_geology'


def coordinates(geometry):
    """yield every (x, y) of a GeoJSON geometry"""

    def walk(part):
        if part and isinstance(part[0], (int, float)):
            yield part
        else:
            for item in part:
                yield from walk(item)

    yield from walk(geometry['coordinates'])


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
    names = [t['name'] for t in db.list_tables(match='bedrock')]

    assert 'public.bedrock' in names
    assert all('bedrock' in name for name in names)


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


@pytest.fixture
def geopackage_table(db):
    try:
        return db.get_table(GEOPACKAGE_TABLE)
    except TableNotFoundError:
        pytest.skip(f'{GEOPACKAGE_TABLE} is not loaded')


def test_geopackage_layer_is_listed(db, geopackage_table):
    # the layer name starts with a digit, which PostgreSQL only accepts
    # quoted; it must survive validation and identifier composition
    assert geopackage_table['srid'] == 27700
    assert geopackage_table['geometry_column'] == 'geom'


def test_clip_geopackage_layer(db, geopackage_table):
    collection = db.clip(geopackage_table, EDINBURGH, limit=50)

    assert collection['numberReturned'] > 0

    for feature in collection['features']:
        for x, y in coordinates(feature['geometry']):
            # clipped to the area of interest, allowing for rounding
            assert -3.2501 <= x <= -3.0999
            assert 55.9199 <= y <= 56.0001


def test_geopackage_layer_keeps_its_attributes(db, geopackage_table):
    collection = db.clip(geopackage_table, EDINBURGH, limit=1,
                         properties=['lex_d'])

    assert list(collection['features'][0]['properties']) == ['lex_d']
