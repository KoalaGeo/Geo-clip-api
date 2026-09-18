# =================================================================
#
# Geo clip API: shipped configuration tests
#
# =================================================================

"""The configuration published in the image must stay drivable by the
environment: a container is configured with variables, not by editing YAML
inside it."""

from pathlib import Path

import pytest

from pygeoapi.config import get_config

from geoclip.db import ClipDatabase

CONFIG = Path(__file__).resolve().parent.parent / 'pygeoapi-config.yml'

PROCESSES = ('clip', 'list-tables')


@pytest.fixture
def load(monkeypatch):
    def _load(**environment):
        monkeypatch.setenv('PYGEOAPI_CONFIG', str(CONFIG))
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        return get_config()

    return _load


def data_block(config, process='clip'):
    return config['resources'][process]['processor']['data']


def test_both_processes_are_published(load):
    config = load()

    for process in PROCESSES:
        assert config['resources'][process]['type'] == 'process'


def test_both_processes_share_one_data_block(load):
    config = load()

    assert data_block(config, 'clip') == data_block(config, 'list-tables')


def test_defaults_without_any_environment(load):
    db = ClipDatabase(data_block(load()))

    assert db.allowed_schemas == ['public']
    assert db.allowed_tables == frozenset()
    assert db.excluded_tables == frozenset()
    assert db.default_limit == 1000
    assert db.max_features == 10000
    assert db.make_valid_source is False


def test_connection_details_come_from_the_environment(load):
    config = load(POSTGRES_HOST='db.example.org', POSTGRES_PORT='6432',
                  POSTGRES_DB='geology', POSTGRES_USER='reader',
                  POSTGRES_PASSWORD='secret')
    db = ClipDatabase(data_block(config))

    assert db.summary == 'db.example.org:6432/geology'


def test_published_tables_come_from_the_environment(load):
    config = load(GEOCLIP_SCHEMAS='public,geology',
                  GEOCLIP_ALLOWED_TABLES='public.boreholes, geology.faults',
                  GEOCLIP_EXCLUDED_TABLES='public.secrets')
    db = ClipDatabase(data_block(config))

    assert db.allowed_schemas == ['public', 'geology']
    assert db.allowed_tables == frozenset(['public.boreholes',
                                           'geology.faults'])
    assert db.is_allowed('public', 'boreholes')
    assert not db.is_allowed('public', 'anything_else')
    assert not db.is_allowed('public', 'secrets')


def test_guard_rails_come_from_the_environment(load):
    config = load(GEOCLIP_DEFAULT_LIMIT='50', GEOCLIP_MAX_FEATURES='500',
                  GEOCLIP_STATEMENT_TIMEOUT='5000',
                  GEOCLIP_COORDINATE_PRECISION='3',
                  GEOCLIP_POOL_MAX='2', GEOCLIP_MAKE_VALID_SOURCE='true')
    db = ClipDatabase(data_block(config))

    assert db.default_limit == 50
    assert db.max_features == 500
    assert db.clamp_limit(10 ** 6) == 500
    assert db.statement_timeout == 5000
    assert db.coordinate_precision == 3
    assert db.pool_max == 2
    assert db.make_valid_source is True
