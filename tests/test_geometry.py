# =================================================================
#
# Geo clip API: clip geometry tests
#
# =================================================================

import pytest

from geoclip.errors import InvalidInputError
from geoclip.geometry import parse_clip_geometry

POLYGON = 'POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))'
MULTIPOLYGON = ('MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)),'
                '((2 2, 3 2, 3 3, 2 3, 2 2)))')


def test_polygon_is_accepted():
    wkt, srid = parse_clip_geometry(POLYGON)

    assert wkt == POLYGON
    assert srid == 4326


def test_multipolygon_is_accepted():
    wkt, srid = parse_clip_geometry(MULTIPOLYGON, srid=27700)

    assert wkt == MULTIPOLYGON
    assert srid == 27700


def test_ewkt_prefix_wins_over_srid_argument():
    wkt, srid = parse_clip_geometry(f'SRID=27700;{POLYGON}', srid=4326)

    assert wkt == POLYGON
    assert srid == 27700


def test_self_intersecting_polygon_is_accepted_for_repair():
    # repaired by ST_MakeValid in the clip query
    bowtie = 'POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))'

    assert parse_clip_geometry(bowtie)[0] == bowtie


@pytest.mark.parametrize('value', [
    'POINT(0 0)',
    'LINESTRING(0 0, 1 1)',
    'GEOMETRYCOLLECTION(POINT(0 0))'
])
def test_non_polygon_types_are_rejected(value):
    with pytest.raises(InvalidInputError, match='POLYGON or MULTIPOLYGON'):
        parse_clip_geometry(value)


def test_empty_geometry_is_rejected():
    with pytest.raises(InvalidInputError, match='empty'):
        parse_clip_geometry('POLYGON EMPTY')


@pytest.mark.parametrize('value', ['', '   ', None, 42])
def test_missing_geometry_is_rejected(value):
    with pytest.raises(InvalidInputError, match='required'):
        parse_clip_geometry(value)


def test_garbage_is_rejected():
    with pytest.raises(InvalidInputError, match='could not parse WKT'):
        parse_clip_geometry('POLYGON((0 0, 1 1)); DROP TABLE users; --')


def test_negative_srid_is_rejected():
    with pytest.raises(InvalidInputError, match='positive'):
        parse_clip_geometry(POLYGON, srid=-1)


def test_non_numeric_srid_is_rejected():
    with pytest.raises(InvalidInputError, match='integer'):
        parse_clip_geometry(POLYGON, srid='british national grid')
