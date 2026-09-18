# =================================================================
#
# Geo clip API: database layer tests
#
# =================================================================

import pytest

from geoclip.db import (ClipDatabase, as_bool, as_list, split_table_name,
                        validate_identifier)
from geoclip.errors import InvalidInputError, TableNotFoundError

from tests.fakes import BEDROCK_ROW, BOREHOLES_ROW, patch_cursor


@pytest.fixture
def db():
    return ClipDatabase({'dbname': 'test', 'max_features': 100,
                         'default_limit': 10})


# ---------------------------------------------------------------- identifiers

@pytest.mark.parametrize('value', [
    'roads', 'os_open_roads', '_x', 'a1$b',
    # OGR layer names routinely start with a digit
    '625k_v5_bedrock_geology'
])
def test_valid_identifiers(value):
    assert validate_identifier(value) == value


@pytest.mark.parametrize('value', [
    'roads; DROP TABLE users',
    'roads"',
    'roads-2',
    '625k geology',
    'public.roads',
    '',
    None,
    'x' * 64
])
def test_invalid_identifiers(value):
    with pytest.raises(InvalidInputError):
        validate_identifier(value)


def test_split_bare_table_name():
    assert split_table_name('roads') == (None, 'roads')


def test_split_qualified_table_name():
    assert split_table_name(' public.roads ') == ('public', 'roads')


@pytest.mark.parametrize('value', ['a.b.c', 'public.roads; --', '', None])
def test_split_rejects_bad_names(value):
    with pytest.raises(InvalidInputError):
        split_table_name(value)


# ------------------------------------------------------------ list settings

@pytest.mark.parametrize('value,expected', [
    (None, []),
    ('', []),
    ([], []),
    ('public', ['public']),
    ('public, geology', ['public', 'geology']),
    ('public,,geology,', ['public', 'geology']),
    (['public', 'geology'], ['public', 'geology']),
    ((1, 2), ['1', '2'])
])
def test_as_list(value, expected):
    assert as_list(value, 'allowed_schemas') == expected


def test_as_list_rejects_other_types():
    with pytest.raises(InvalidInputError, match='comma separated'):
        as_list({'schema': 'public'}, 'allowed_schemas')


@pytest.mark.parametrize('value,expected', [
    (None, False), ('', False), (True, True), (False, False),
    ('true', True), ('TRUE', True), ('yes', True), ('1', True),
    ('false', False), ('no', False), ('0', False)
])
def test_as_bool(value, expected):
    assert as_bool(value, 'make_valid_source') is expected


def test_as_bool_rejects_nonsense():
    with pytest.raises(InvalidInputError, match='boolean'):
        as_bool('perhaps', 'make_valid_source')


def test_list_settings_accept_environment_style_strings():
    # pygeoapi expands ${VAR} to a scalar, so lists arrive comma separated
    db = ClipDatabase({
        'allowed_schemas': 'public, geology',
        'allowed_tables': 'public.boreholes,bedrock',
        'excluded_tables': '',
        'make_valid_source': 'true'
    })

    assert db.allowed_schemas == ['public', 'geology']
    assert db.allowed_tables == frozenset(['public.boreholes', 'bedrock'])
    assert db.excluded_tables == frozenset()
    assert db.make_valid_source is True


# -------------------------------------------------------------- configuration

def test_forbidden_schemas_are_dropped():
    db = ClipDatabase({'allowed_schemas': ['public', 'pg_catalog']})

    assert db.allowed_schemas == ['public']


def test_configuration_of_only_forbidden_schemas_fails():
    with pytest.raises(InvalidInputError):
        ClipDatabase({'allowed_schemas': ['pg_catalog']})


def test_limit_defaults_and_ceiling(db):
    assert db.clamp_limit(None) == 10
    assert db.clamp_limit(5) == 5
    assert db.clamp_limit('7') == 7
    assert db.clamp_limit(10 ** 6) == 100


@pytest.mark.parametrize('value', [0, -1, 'many'])
def test_bad_limits_are_rejected(db, value):
    with pytest.raises(InvalidInputError):
        db.clamp_limit(value)


def test_allow_and_deny_lists():
    db = ClipDatabase({'allowed_tables': ['public.boreholes', 'bedrock'],
                       'excluded_tables': ['public.bedrock']})

    assert db.is_allowed('public', 'boreholes')
    assert not db.is_allowed('public', 'secrets')
    # excluded wins over allowed
    assert not db.is_allowed('public', 'bedrock')


def test_no_allow_list_permits_any_published_table(db):
    assert db.is_allowed('public', 'anything')


# ------------------------------------------------------------------ catalogue

def test_list_tables_adds_qualified_name(db):
    patch_cursor(db, [BOREHOLES_ROW, BEDROCK_ROW])

    tables = db.list_tables()

    assert [t['name'] for t in tables] == ['public.boreholes',
                                           'public.bedrock']


def test_list_tables_applies_match_filter(db):
    patch_cursor(db, [BOREHOLES_ROW, BEDROCK_ROW])

    tables = db.list_tables(match='BORE')

    assert [t['name'] for t in tables] == ['public.boreholes']


def test_list_tables_applies_deny_list():
    db = ClipDatabase({'excluded_tables': ['public.bedrock']})
    patch_cursor(db, [BOREHOLES_ROW, BEDROCK_ROW])

    assert [t['name'] for t in db.list_tables()] == ['public.boreholes']


def test_list_tables_rejects_unpublished_schema(db):
    patch_cursor(db, [])

    with pytest.raises(InvalidInputError, match='not published'):
        db.list_tables(schema='secret')


def test_list_tables_passes_allowed_schemas_as_parameter(db):
    cursor = patch_cursor(db, [])

    db.list_tables()

    assert cursor.last_parameters == {'schemas': ['public']}


def test_get_table_resolves_bare_name(db):
    patch_cursor(db, [BOREHOLES_ROW, BEDROCK_ROW])

    assert db.get_table('boreholes')['name'] == 'public.boreholes'


def test_get_table_rejects_unknown_table(db):
    patch_cursor(db, [BOREHOLES_ROW])

    with pytest.raises(TableNotFoundError, match='not available'):
        db.get_table('public.users')


def test_get_table_needs_geometry_column_when_ambiguous(db):
    other_geom = dict(BOREHOLES_ROW, geometry_column='centroid')
    patch_cursor(db, [BOREHOLES_ROW, other_geom])

    with pytest.raises(InvalidInputError, match='more than one geometry'):
        db.get_table('boreholes')

    patch_cursor(db, [BOREHOLES_ROW, other_geom])

    table = db.get_table('boreholes', geometry_column='centroid')
    assert table['geometry_column'] == 'centroid'


def test_get_table_flags_ambiguous_schemas(db):
    db = ClipDatabase({'allowed_schemas': ['public', 'geology']})
    patch_cursor(db, [BOREHOLES_ROW, dict(BOREHOLES_ROW, schema='geology')])

    with pytest.raises(InvalidInputError, match='ambiguous'):
        db.get_table('boreholes')


# ------------------------------------------------------------------- clipping

CLIP_WKT = 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))'

FEATURE = {
    'type': 'Feature',
    'geometry': {'type': 'Point', 'coordinates': [0.5, 0.5]},
    'properties': {'id': 1, 'name': 'BH001'}
}


def _collection(features):
    return [{'collection': {'type': 'FeatureCollection',
                            'features': list(features)}}]


def test_clip_returns_feature_collection(db):
    patch_cursor(db, _collection([FEATURE]))

    collection = db.clip(BOREHOLES_ROW, CLIP_WKT)

    assert collection['type'] == 'FeatureCollection'
    assert collection['features'] == [FEATURE]
    assert collection['numberReturned'] == 1
    assert collection['truncated'] is False
    assert 'crs' not in collection


def test_clip_passes_bound_parameters(db):
    cursor = patch_cursor(db, _collection([]))

    db.clip(BOREHOLES_ROW, CLIP_WKT, wkt_srid=27700, limit=5)

    parameters = cursor.last_parameters
    assert parameters['wkt'] == CLIP_WKT
    assert parameters['wkt_srid'] == 27700
    assert parameters['output_srid'] == 4326
    assert parameters['limit'] == 5


def test_clip_clamps_the_limit(db):
    cursor = patch_cursor(db, _collection([]))

    db.clip(BOREHOLES_ROW, CLIP_WKT, limit=10 ** 9)

    assert cursor.last_parameters['limit'] == 100


def test_clip_flags_truncation(db):
    patch_cursor(db, _collection([FEATURE, FEATURE]))

    collection = db.clip(BOREHOLES_ROW, CLIP_WKT, limit=2)

    assert collection['truncated'] is True


def test_clip_reports_non_wgs84_output_crs(db):
    patch_cursor(db, _collection([]))

    collection = db.clip(BEDROCK_ROW, CLIP_WKT, output_srid=27700)

    assert collection['crs']['properties']['name'] == \
        'urn:ogc:def:crs:EPSG::27700'


def test_clip_handles_empty_result(db):
    patch_cursor(db, [{'collection': None}])

    collection = db.clip(BOREHOLES_ROW, CLIP_WKT)

    assert collection['features'] == []
    assert collection['numberReturned'] == 0


def test_clip_rejects_unknown_properties(db):
    patch_cursor(db, [{'column': 'id'}, {'column': 'name'},
                      {'column': 'geom'}])

    with pytest.raises(InvalidInputError, match='unknown column'):
        db.clip(BOREHOLES_ROW, CLIP_WKT, properties=['id', 'salary'])


def test_clip_rejects_injected_property_names(db):
    with pytest.raises(InvalidInputError):
        db.clip(BOREHOLES_ROW, CLIP_WKT, properties=['id, (SELECT 1)'])


def test_clip_query_is_composed_not_interpolated(db):
    # the table name never appears in the query as raw text: it is quoted
    # by psycopg2.sql.Identifier at execution time
    query = db._build_clip_query(BOREHOLES_ROW)

    assert query.seq  # a psycopg2 Composed, not a string
