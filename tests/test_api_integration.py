# =================================================================
#
# Geo clip API: HTTP service tests against PostGIS
#
# =================================================================

"""End-to-end tests for the download service against a real database.

Skipped unless ``GEOCLIP_TEST_DSN`` is set. The geology GeoPackage loaded
by the compose stack is used where it is present.
"""

import os

import pytest

fastapi = pytest.importorskip('fastapi', reason='needs fastapi')
fiona = pytest.importorskip('fiona', reason='needs fiona to read downloads')

from fastapi.testclient import TestClient    # noqa: E402

from geoclip.api.app import create_app       # noqa: E402

DSN = os.environ.get('GEOCLIP_TEST_DSN')

pytestmark = pytest.mark.skipif(not DSN, reason='GEOCLIP_TEST_DSN is not set')

EDINBURGH = [-3.25, 55.92, -3.10, 56.00]
GEOLOGY = 'public.625k_v5_bedrock_geology'


@pytest.fixture(scope='module')
def client():
    # set through monkeypatch so the variable does not leak into the rest
    # of the suite, where it would override the POSTGRES_* settings
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv('GEOCLIP_DSN', DSN)

        with TestClient(create_app()) as test_client:
            yield test_client


@pytest.fixture
def geology(client):
    if client.get(f'/tables/{GEOLOGY}').status_code != 200:
        pytest.skip('the geology GeoPackage is not loaded')

    return GEOLOGY


def test_readiness_against_a_real_database(client):
    assert client.get('/readyz').json()['status'] == 'ready'


def test_demo_tables_are_listed(client):
    names = [t['name'] for t in client.get('/tables').json()['tables']]

    assert 'public.boreholes' in names


def test_clip_returns_the_demo_boreholes(client):
    body = client.post('/clip', json={'table': 'public.boreholes',
                                      'bbox': EDINBURGH}).json()

    assert {f['properties']['name'] for f in body['features']} == \
        {'BH001', 'BH002'}


def test_estimate_matches_what_the_clip_returns(client):
    payload = {'table': 'public.boreholes', 'bbox': EDINBURGH}
    estimate = client.post('/estimate', json=payload).json()
    clipped = client.post('/clip', json=payload).json()

    assert estimate['rows'] == clipped['numberReturned']
    assert estimate['requested_area_km2'] > 0


def test_estimate_is_close_enough_to_the_real_size(client, geology):
    payload = {'table': geology, 'bbox': EDINBURGH}
    estimate = client.post('/estimate', json=payload).json()
    actual = len(client.post('/clip', json=payload).content)

    # the size model is documented as being within roughly 20%
    assert 0.5 < estimate['estimated_bytes'] / actual < 2.0


@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_downloads_open_in_ogr(client, geology, tmp_path, fmt):
    response = client.post('/clip', json={'table': geology,
                                          'bbox': EDINBURGH,
                                          'format': fmt})

    assert response.status_code == 200

    path = tmp_path / f'download.{fmt}'
    path.write_bytes(response.content)

    with fiona.open(path) as source:
        assert len(source) == int(response.headers['x-geoclip-rows'])
        assert str(source.crs) == 'EPSG:4326'


def test_output_srid_reaches_the_file(client, geology, tmp_path):
    response = client.post('/clip', json={'table': geology,
                                          'bbox': EDINBURGH,
                                          'format': 'gpkg',
                                          'output_srid': 27700})

    path = tmp_path / 'bng.gpkg'
    path.write_bytes(response.content)

    with fiona.open(path) as source:
        assert str(source.crs) == 'EPSG:27700'


def test_a_real_order_over_the_limit_is_refused(client, geology):
    response = client.post('/clip', json={'table': geology,
                                          'bbox': EDINBURGH, 'limit': 2})

    assert response.status_code == 413
    assert response.json()['rows'] > 2


def test_simplify_reaches_the_database(client, geology):
    plain = client.post('/clip', json={'table': geology,
                                       'bbox': EDINBURGH}).content
    simplified = client.post('/clip', json={'table': geology,
                                            'bbox': EDINBURGH,
                                            'simplify': True}).content

    assert len(simplified) < len(plain)
