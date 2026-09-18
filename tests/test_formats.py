# =================================================================
#
# Geo clip API: output format tests
#
# =================================================================

"""Tests for the GeoPackage and FlatGeobuf writers.

The files are read back with fiona, so a format that writes but cannot be
opened fails here rather than in a user's GIS.
"""

import pytest

from geoclip.errors import InvalidInputError
from geoclip.formats import (DEFAULT_FORMAT, FormatError, build_schema,
                             extension, layer_name, media_type, parse_format,
                             write_collection)

fiona = pytest.importorskip('fiona', reason='needs fiona to read files back')

POINT = {
    'type': 'Feature',
    'geometry': {'type': 'Point', 'coordinates': [-3.19, 55.95]},
    'properties': {'name': 'BH001', 'depth_m': 42.5, 'deep': True,
                   'note': None}
}

POLYGON = {
    'type': 'Feature',
    'geometry': {'type': 'Polygon',
                 'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
    'properties': {'name': 'BH002', 'depth_m': 18, 'deep': False,
                   'note': 'x'}
}


def collection(*features):
    return {'type': 'FeatureCollection', 'features': list(features)}


def read_back(tmp_path, data, fmt):
    path = tmp_path / f'out.{extension(fmt)}'
    path.write_bytes(data)

    with fiona.open(path) as source:
        return {
            'name': source.name,
            'crs': str(source.crs) if source.crs else None,
            'schema': source.schema,
            'features': [dict(feature['properties']) for feature in source]
        }


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize('value,expected', [
    (None, 'geojson'), ('', 'geojson'), ('geojson', 'geojson'),
    ('GeoJSON', 'geojson'), ('json', 'geojson'), ('gpkg', 'gpkg'),
    ('GeoPackage', 'gpkg'), ('fgb', 'fgb'), ('flatgeobuf', 'fgb'),
    ('  FGB  ', 'fgb')
])
def test_parse_format(value, expected):
    assert parse_format(value) == expected


@pytest.mark.parametrize('value', ['shp', 'kml', 'csv', 42, ['gpkg']])
def test_unsupported_formats_are_rejected(value):
    with pytest.raises(InvalidInputError):
        parse_format(value)


def test_media_types():
    assert media_type(DEFAULT_FORMAT) == 'application/geo+json'
    assert media_type('gpkg') == 'application/geopackage+sqlite3'
    assert media_type('fgb') == 'application/flatgeobuf'


@pytest.mark.parametrize('value,expected', [
    ('public.625k_v5_faults', '625k_v5_faults'),
    ('boreholes', 'boreholes'),
    ('public.odd name!', 'odd_name_'),
    (None, 'clip'),
    ('', 'clip')
])
def test_layer_names(value, expected):
    assert layer_name(value) == expected


# ------------------------------------------------------------------- schema

def test_schema_infers_types():
    schema = build_schema([POINT, POLYGON])

    assert schema['geometry'] == 'Unknown'
    assert schema['properties'] == {
        'name': 'str',
        'depth_m': 'float',   # 42.5 and 18 widen to float
        'deep': 'int',        # fiona has no boolean
        'note': 'str'
    }


def test_schema_keeps_columns_that_are_always_null():
    schema = build_schema([{'properties': {'a': None}, 'geometry': None}])

    assert schema['properties'] == {'a': 'str'}


def test_schema_widens_mixed_types_to_string():
    features = [{'properties': {'a': 1}}, {'properties': {'a': 'one'}}]

    assert build_schema(features)['properties'] == {'a': 'str'}


# ------------------------------------------------------------------ writing

@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_round_trip(tmp_path, fmt):
    data = write_collection(collection(POINT, POLYGON), fmt, 4326,
                            'public.boreholes')
    result = read_back(tmp_path, data, fmt)

    assert result['name'] == 'boreholes'
    assert result['crs'] == 'EPSG:4326'
    assert len(result['features']) == 2

    # FlatGeobuf reorders features into its spatial index, so compare sets
    assert {feature['name'] for feature in result['features']} == \
        {'BH001', 'BH002'}


@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_values_survive(tmp_path, fmt):
    data = write_collection(collection(POINT), fmt, 4326, 'boreholes')
    written = read_back(tmp_path, data, fmt)['features'][0]

    assert written['name'] == 'BH001'
    assert written['depth_m'] == 42.5
    assert written['deep'] == 1       # booleans are written as integers
    assert written['note'] is None


@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_mixed_geometry_types_are_written(tmp_path, fmt):
    multipolygon = {
        'type': 'Feature',
        'properties': {'name': 'a'},
        'geometry': {'type': 'MultiPolygon', 'coordinates': [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]]}
    }

    data = write_collection(collection(POLYGON, multipolygon), fmt, 4326,
                            'bedrock')

    assert len(read_back(tmp_path, data, fmt)['features']) == 2


@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_other_crs_is_recorded(tmp_path, fmt):
    feature = {
        'type': 'Feature',
        'properties': {'name': 'a'},
        'geometry': {'type': 'Point', 'coordinates': [325000, 675000]}
    }

    data = write_collection(collection(feature), fmt, 27700, 'boreholes')

    assert read_back(tmp_path, data, fmt)['crs'] == 'EPSG:27700'


@pytest.mark.parametrize('fmt', ['gpkg', 'fgb'])
def test_empty_collection_is_still_a_valid_file(tmp_path, fmt):
    data = write_collection(collection(), fmt, 4326, 'boreholes')

    assert read_back(tmp_path, data, fmt)['features'] == []


def test_table_without_a_crs_writes_a_file_without_one(tmp_path):
    data = write_collection(collection(POINT), 'gpkg', None, 'boreholes')

    assert read_back(tmp_path, data, 'gpkg')['crs'] is None


def test_geojson_is_not_written_through_ogr():
    with pytest.raises(FormatError):
        write_collection(collection(POINT), 'geojson', 4326, 'boreholes')
