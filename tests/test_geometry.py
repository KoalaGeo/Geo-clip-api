# =================================================================
#
# Geo clip API: clip geometry tests
#
# =================================================================

import json

import pytest

from geoclip.errors import InvalidInputError
from geoclip.geometry import (clip_area, parse_bbox, parse_clip_geometry,
                              parse_geojson)

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


# ------------------------------------------------------------------- GeoJSON

GEOJSON_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
}

GEOJSON_POLYGON_2 = {
    'type': 'Polygon',
    'coordinates': [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]]
}


def feature(geometry):
    return {'type': 'Feature', 'properties': {}, 'geometry': geometry}


def test_geojson_geometry_is_accepted():
    wkt, srid = parse_geojson(GEOJSON_POLYGON)

    assert wkt.startswith('POLYGON')
    assert srid == 4326


def test_geojson_feature_is_unwrapped():
    assert parse_geojson(feature(GEOJSON_POLYGON))[0].startswith('POLYGON')


def test_geojson_feature_collection_is_merged():
    collection = {
        'type': 'FeatureCollection',
        'features': [feature(GEOJSON_POLYGON), feature(GEOJSON_POLYGON_2)]
    }

    assert parse_geojson(collection)[0].startswith('MULTIPOLYGON')


def test_overlapping_features_merge_into_one_polygon():
    overlapping = {
        'type': 'FeatureCollection',
        'features': [
            feature(GEOJSON_POLYGON),
            feature({'type': 'Polygon',
                     'coordinates': [[[0.5, 0], [1.5, 0], [1.5, 1], [0.5, 1],
                                      [0.5, 0]]]})
        ]
    }

    assert parse_geojson(overlapping)[0].startswith('POLYGON')


def test_geojson_accepts_a_json_string():
    assert parse_geojson(json.dumps(GEOJSON_POLYGON))[0].startswith('POLYGON')


def test_geojson_srid_is_honoured():
    assert parse_geojson(GEOJSON_POLYGON, srid=27700)[1] == 27700


@pytest.mark.parametrize('value,message', [
    ({'type': 'Point', 'coordinates': [0, 0]}, 'POLYGON or MULTIPOLYGON'),
    ({'type': 'FeatureCollection', 'features': []}, 'no features'),
    ({'type': 'Feature', 'properties': {}}, 'no geometry'),
    ({'coordinates': [0, 0]}, 'no "type"'),
    ('not json at all', 'could not parse GeoJSON'),
    (42, 'geometry must be a GeoJSON'),
    ({'type': 'Polygon', 'coordinates': 'nonsense'},
     'could not parse GeoJSON geometry')
])
def test_bad_geojson_is_rejected(value, message):
    with pytest.raises(InvalidInputError, match=message):
        parse_geojson(value)


# ---------------------------------------------------------------------- bbox

def test_bbox_becomes_a_polygon():
    wkt, srid = parse_bbox([-3.25, 55.92, -3.10, 56.00])

    assert wkt.startswith('POLYGON')
    assert srid == 4326


def test_bbox_accepts_a_comma_separated_string():
    assert parse_bbox('-3.25, 55.92, -3.10, 56.00')[0] == \
        parse_bbox([-3.25, 55.92, -3.10, 56.00])[0]


def test_bbox_covers_the_requested_extent():
    from shapely import wkt as shapely_wkt

    geometry = shapely_wkt.loads(parse_bbox([-3.25, 55.92, -3.10, 56.0])[0])

    assert geometry.bounds == (-3.25, 55.92, -3.10, 56.0)


@pytest.mark.parametrize('value', [
    [0, 0, 1],
    [0, 0, 1, 1, 1],
    'nope',
    [0, 0, 'east', 1],
    [1, 0, 0, 1],
    [0, 1, 1, 0],
    [0, 0, 0, 1],
    [float('nan'), 0, 1, 1]
])
def test_bad_bbox_is_rejected(value):
    with pytest.raises(InvalidInputError):
        parse_bbox(value)


# ----------------------------------------------------------------- clip area

def test_clip_area_prefers_nothing_and_requires_one():
    with pytest.raises(InvalidInputError, match='clip area is required'):
        clip_area()


def test_clip_area_rejects_more_than_one():
    with pytest.raises(InvalidInputError, match='only one clip area'):
        clip_area(wkt=POLYGON, bbox=[0, 0, 1, 1])


def test_clip_area_dispatches_to_each_parser():
    assert clip_area(wkt=POLYGON)[0] == POLYGON
    assert clip_area(geometry=GEOJSON_POLYGON)[0].startswith('POLYGON')
    assert clip_area(bbox=[0, 0, 1, 1])[0].startswith('POLYGON')
    assert clip_area(wkt=f'SRID=27700;{POLYGON}')[1] == 27700
