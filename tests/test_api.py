# =================================================================
#
# Geo clip API: HTTP service tests
#
# =================================================================

"""Tests for the standalone FastAPI download service.

These run against a fake database, so they cover the HTTP behaviour the
service exists for: content negotiation, filenames, the limit refusing an
order, and problem responses. The end-to-end behaviour against PostGIS is
covered by tests/test_integration.py.
"""

import pytest

fastapi = pytest.importorskip('fastapi', reason='needs fastapi')

from fastapi.testclient import TestClient          # noqa: E402

from geoclip.api.app import create_app, get_database  # noqa: E402
from geoclip.errors import DatabaseError              # noqa: E402

from tests.fakes import FakeDatabase                  # noqa: E402

BBOX = [-3.30, 55.90, -3.05, 56.02]
POLYGON = 'POLYGON((-3.30 55.90, -3.05 55.90, -3.05 56.02, -3.30 56.02, ' \
          '-3.30 55.90))'
GEOJSON = {
    'type': 'Polygon',
    'coordinates': [[[-3.30, 55.90], [-3.05, 55.90], [-3.05, 56.02],
                     [-3.30, 56.02], [-3.30, 55.90]]]
}
TABLE = 'public.boreholes'


@pytest.fixture
def database():
    return FakeDatabase()


@pytest.fixture
def client(database):
    app = create_app()
    app.dependency_overrides[get_database] = lambda: database

    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ service

def test_root_describes_the_service(client):
    body = client.get('/').json()

    assert sorted(body['formats']) == ['fgb', 'geojson', 'gpkg']
    assert body['links']['clip'] == '/clip'


def test_liveness_does_not_touch_the_database(client, database):
    database.ready = False

    assert client.get('/healthz').status_code == 200


def test_readiness_reports_the_database(client):
    assert client.get('/readyz').json()['status'] == 'ready'


def test_readiness_fails_when_the_database_is_down(client, database):
    database.ready = False
    response = client.get('/readyz')

    assert response.status_code == 503
    assert response.headers['content-type'].startswith(
        'application/problem+json')


# ------------------------------------------------------------------- tables

def test_tables_are_listed(client):
    body = client.get('/tables').json()

    assert body['count'] == 2
    assert body['tables'][0]['name'] == 'public.boreholes'
    assert body['tables'][0]['schema'] == 'public'
    assert body['schemas'] == ['public']


def test_table_filters_are_passed_through(client, database):
    client.get('/tables', params={'schema': 'public', 'match': 'bore'})

    assert ('list_tables', 'public', 'bore') in database.calls


def test_one_table(client):
    body = client.get('/tables/boreholes').json()

    assert body['name'] == 'public.boreholes'
    assert body['geometry_column'] == 'geom'


def test_unknown_table_is_a_problem_response(client):
    response = client.get('/tables/nope')

    assert response.status_code == 404
    assert response.json()['title'] == 'No such table'


# ----------------------------------------------------------------- estimate

def test_estimate_sizes_an_order(client):
    body = client.post('/estimate', json={'table': TABLE,
                                          'bbox': BBOX}).json()

    assert body['rows'] == 2
    assert body['requested_area_km2'] == 208.62
    assert body['covered_area_km2'] == 123.94
    assert body['format'] == 'geojson'
    assert body['within_limit'] is True


@pytest.mark.parametrize('fmt,expected', [
    ('geojson', 4000), ('gpkg', 98304), ('fgb', 2480)
])
def test_estimate_scales_to_the_format(client, fmt, expected):
    body = client.post('/estimate', json={'table': TABLE, 'bbox': BBOX,
                                          'format': fmt}).json()

    assert body['estimated_bytes'] == expected


def test_estimate_reports_being_over_the_limit(client):
    body = client.post('/estimate', json={'table': TABLE, 'bbox': BBOX,
                                          'limit': 1}).json()

    assert body['within_limit'] is False


def test_estimate_can_ask_for_exact_area(client, database):
    client.post('/estimate', json={'table': TABLE, 'bbox': BBOX,
                                   'exact_area': True})

    assert ('estimate', 'public.boreholes', True) in database.calls


# --------------------------------------------------------------------- clip

def test_clip_returns_geojson_by_default(client):
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX})

    assert response.status_code == 200
    assert response.headers['content-type'].startswith(
        'application/geo+json')
    assert response.json()['type'] == 'FeatureCollection'
    assert response.headers['x-geoclip-rows'] == '2'
    assert response.headers['x-geoclip-truncated'] == 'false'


@pytest.mark.parametrize('area', [
    {'bbox': BBOX}, {'wkt': POLYGON}, {'geometry': GEOJSON}
])
def test_every_clip_area_input_works(client, area):
    response = client.post('/clip', json={'table': TABLE, **area})

    assert response.status_code == 200


@pytest.mark.parametrize('fmt,media', [
    ('gpkg', 'application/geopackage+sqlite3'),
    ('fgb', 'application/flatgeobuf')
])
def test_binary_downloads_carry_a_filename(client, fmt, media):
    pytest.importorskip('fiona')
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX,
                                          'format': fmt})

    assert response.status_code == 200
    assert response.headers['content-type'] == media
    disposition = response.headers['content-disposition']
    assert disposition.startswith('attachment;')
    assert disposition.endswith(f'.{fmt}"')
    assert 'boreholes' in disposition


@pytest.mark.parametrize('accept,expected', [
    ('application/geo+json', 'application/geo+json'),
    ('application/geopackage+sqlite3', 'application/geopackage+sqlite3'),
    ('application/flatgeobuf', 'application/flatgeobuf'),
    ('*/*', 'application/geo+json')
])
def test_accept_header_selects_the_format(client, accept, expected):
    pytest.importorskip('fiona')
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX},
                           headers={'Accept': accept})

    assert response.headers['content-type'] == expected


def test_the_format_field_beats_the_accept_header(client):
    response = client.post('/clip',
                           json={'table': TABLE, 'bbox': BBOX,
                                 'format': 'geojson'},
                           headers={'Accept': 'application/flatgeobuf'})

    assert response.headers['content-type'].startswith(
        'application/geo+json')


# -------------------------------------------------------------- the limit

def test_an_order_over_the_limit_is_refused(client):
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX,
                                          'limit': 1})

    assert response.status_code == 413
    body = response.json()
    assert body['rows'] == 2
    assert body['limit'] == 1
    assert 'truncate' in body['detail']


def test_truncation_is_opt_in(client):
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX,
                                          'limit': 1,
                                          'on_limit': 'truncate'})

    assert response.status_code == 200
    assert response.headers['x-geoclip-rows'] == '1'
    assert response.headers['x-geoclip-truncated'] == 'true'


def test_the_limit_check_costs_one_count(client, database):
    client.post('/clip', json={'table': TABLE, 'bbox': BBOX})

    assert [call[0] for call in database.calls] == ['count_features', 'clip']


def test_truncating_skips_the_count(client, database):
    client.post('/clip', json={'table': TABLE, 'bbox': BBOX,
                               'on_limit': 'truncate'})

    assert [call[0] for call in database.calls] == ['clip']


# ------------------------------------------------------------------- errors

@pytest.mark.parametrize('body,status,title', [
    ({'table': TABLE}, 400, 'Invalid request'),
    ({'table': TABLE, 'bbox': BBOX, 'wkt': POLYGON}, 400, 'Invalid request'),
    ({'table': TABLE, 'bbox': [1, 1, 0, 0]}, 400, 'Invalid request'),
    ({'table': TABLE, 'bbox': BBOX, 'format': 'shp'}, 400, 'Invalid request'),
    ({'table': 'nope', 'bbox': BBOX}, 404, 'No such table'),
    ({'table': TABLE, 'geometry': {'type': 'Point', 'coordinates': [0, 0]}},
     400, 'Invalid request')
])
def test_bad_requests_are_problem_responses(client, body, status, title):
    response = client.post('/clip', json=body)

    assert response.status_code == status
    assert response.headers['content-type'].startswith(
        'application/problem+json')
    assert response.json()['title'] == title
    assert response.json()['status'] == status


def test_database_failures_are_503(client, database):
    database.error = DatabaseError('connection refused')
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX})

    assert response.status_code == 503
    assert response.json()['title'] == 'Database unavailable'


def test_unknown_fields_are_rejected(client):
    response = client.post('/clip', json={'table': TABLE, 'bbox': BBOX,
                                          'fromat': 'gpkg'})

    assert response.status_code == 422


# ------------------------------------------------------------------ openapi

def test_openapi_declares_every_download_media_type(client):
    content = client.get('/openapi.json').json()[
        'paths']['/clip']['post']['responses']['200']['content']

    # the pygeoapi plugin cannot do this: its generated document declares
    # one media type, so a console renders a GeoPackage as text
    assert set(content) == {'application/geo+json',
                            'application/geopackage+sqlite3',
                            'application/flatgeobuf'}
    assert content['application/geopackage+sqlite3']['schema']['format'] == \
        'binary'


# ------------------------------------------------------- GET, as a link

def test_get_clip_works_as_a_url(client):
    response = client.get('/clip', params={'table': TABLE,
                                           'bbox': '-3.30,55.90,-3.05,56.02'})

    assert response.status_code == 200
    assert response.json()['type'] == 'FeatureCollection'


def test_get_clip_takes_wkt_too(client):
    response = client.get('/clip', params={'table': TABLE, 'wkt': POLYGON})

    assert response.status_code == 200


@pytest.mark.parametrize('fmt,media', [
    ('gpkg', 'application/geopackage+sqlite3'),
    ('fgb', 'application/flatgeobuf'),
    ('geojson', 'application/geo+json')
])
def test_get_clip_formats(client, fmt, media):
    pytest.importorskip('fiona')
    response = client.get('/clip', params={'table': TABLE,
                                           'bbox': '-3.30,55.90,-3.05,56.02',
                                           'format': fmt})

    assert response.headers['content-type'] == media


def test_get_clip_passes_the_options_through(client, database):
    client.get('/clip', params={'table': TABLE,
                                'bbox': '-3.30,55.90,-3.05,56.02',
                                'properties': 'name, depth_m',
                                'clip': 'false', 'simplify': 'true',
                                'output_srid': 27700,
                                'on_limit': 'truncate'})

    kwargs = [c for c in database.calls if c[0] == 'clip'][0][2]
    assert kwargs['properties'] == ['name', 'depth_m']
    assert kwargs['clip_geometries'] is False
    assert kwargs['simplify'] is True
    assert kwargs['output_srid'] == 27700


def test_get_clip_refuses_an_order_over_the_limit(client):
    response = client.get('/clip', params={'table': TABLE,
                                           'bbox': '-3.30,55.90,-3.05,56.02',
                                           'limit': 1})

    assert response.status_code == 413


@pytest.mark.parametrize('bbox', ['1,2,3', 'a,b,c,d', '1,2,3,4,5'])
def test_get_clip_rejects_a_bad_bbox(client, bbox):
    response = client.get('/clip', params={'table': TABLE, 'bbox': bbox})

    assert response.status_code == 400
    assert response.json()['title'] == 'Invalid request'


def test_get_clip_rejects_an_unknown_format(client):
    # an enum parameter, so this one is caught before the handler
    response = client.get('/clip', params={'table': TABLE,
                                           'bbox': '-3.30,55.90,-3.05,56.02',
                                           'format': 'shp'})

    assert response.status_code == 422


def test_get_estimate_works_as_a_url(client):
    body = client.get('/estimate', params={'table': TABLE,
                                           'bbox': '-3.30,55.90,-3.05,56.02',
                                           'format': 'fgb'}).json()

    assert body['rows'] == 2
    assert body['format'] == 'fgb'


def test_get_and_post_agree(client):
    params = {'table': TABLE, 'bbox': '-3.30,55.90,-3.05,56.02'}
    from_get = client.get('/estimate', params=params).json()
    from_post = client.post('/estimate', json={'table': TABLE,
                                               'bbox': [-3.30, 55.90,
                                                        -3.05, 56.02]}).json()

    assert from_get == from_post


# the drop-downs in the API console come from enum *parameters*; a JSON
# body is rendered as a text area whatever its schema says
@pytest.mark.parametrize('name,values', [
    ('format', ['geojson', 'gpkg', 'fgb']),
    ('on_limit', ['error', 'truncate'])
])
def test_enum_query_parameters_are_declared(client, name, values):
    spec = client.get('/openapi.json').json()
    parameters = {p['name']: p
                  for p in spec['paths']['/clip']['get']['parameters']}
    schema = parameters[name]['schema']

    reference = schema.get('$ref') or [
        option.get('$ref') for option in schema.get('anyOf', [])
        if option.get('$ref')][0]
    enum = spec['components']['schemas'][reference.split('/')[-1]]['enum']

    assert enum == values
