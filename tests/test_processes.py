# =================================================================
#
# Geo clip API: processor tests
#
# =================================================================

import pytest

from pygeoapi.process.base import ProcessorExecuteError

from geoclip.errors import DatabaseError, TableNotFoundError
from geoclip.processes.clip import (PROCESS_METADATA, ClipProcessor, as_bool,
                                    as_property_list)
from geoclip.processes.list_tables import ListTablesProcessor

from tests.fakes import BEDROCK_ROW, BOREHOLES_ROW

POLYGON = 'POLYGON((-3.25 55.92, -3.10 55.92, -3.10 56.00, -3.25 56.00, ' \
          '-3.25 55.92))'

PROCESSOR_DEF = {
    'name': 'geoclip.processes.clip.ClipProcessor',
    'data': {'dbname': 'test', 'max_features': 50}
}

COLLECTION = {
    'type': 'FeatureCollection',
    'features': [],
    'numberReturned': 0,
    'truncated': False
}


class FakeDB:
    """stands in for `geoclip.db.ClipDatabase`"""

    summary = 'fake'
    allowed_schemas = ['public']
    default_output_srid = 4326

    def __init__(self, tables=None, error=None):
        self.tables = tables if tables is not None else [BOREHOLES_ROW,
                                                         BEDROCK_ROW]
        self.error = error
        self.clip_calls = []
        self.list_calls = []

    def get_table(self, name, geometry_column=None):
        if self.error:
            raise self.error
        for table in self.tables:
            if name in (table['table'], f"{table['schema']}.{table['table']}"):
                return dict(table, name=f"{table['schema']}.{table['table']}")
        raise TableNotFoundError(f'table {name!r} is not available')

    def clip(self, table, wkt, **kwargs):
        if self.error:
            raise self.error
        self.clip_calls.append(dict(table=table, wkt=wkt, **kwargs))
        return dict(COLLECTION)

    def list_tables(self, schema=None, match=None):
        if self.error:
            raise self.error
        self.list_calls.append({'schema': schema, 'match': match})
        return [dict(t, name=f"{t['schema']}.{t['table']}")
                for t in self.tables]


def make_clip_processor(db=None):
    processor = ClipProcessor(dict(PROCESSOR_DEF))
    processor.db = db or FakeDB()

    return processor


def make_list_processor(db=None):
    processor = ListTablesProcessor({
        'name': 'geoclip.processes.list_tables.ListTablesProcessor',
        'data': {'dbname': 'test'}
    })
    processor.db = db or FakeDB()

    return processor


# ------------------------------------------------------------------- metadata

def test_process_metadata_declares_required_inputs():
    inputs = PROCESS_METADATA['inputs']

    assert PROCESS_METADATA['id'] == 'clip'
    assert inputs['table']['minOccurs'] == 1
    assert 'featureCollection' in PROCESS_METADATA['outputs']

    # the clip area comes from exactly one of these, so none of them can be
    # declared required on its own
    for name in ('wkt', 'geometry', 'bbox'):
        assert inputs[name]['minOccurs'] == 0
        assert 'one of wkt, geometry or bbox' in \
            inputs[name]['description'].lower()


# ------------------------------------------------------------------ coercions

@pytest.mark.parametrize('value,expected', [
    (None, True), (True, True), (False, False), ('true', True),
    ('False', False), ('1', True), ('no', False)
])
def test_as_bool(value, expected):
    assert as_bool(value, 'clip') is expected


def test_as_bool_rejects_nonsense():
    with pytest.raises(ProcessorExecuteError):
        as_bool('maybe', 'clip')


def test_as_property_list_accepts_csv_and_arrays():
    assert as_property_list('id, name') == ['id', 'name']
    assert as_property_list(['id']) == ['id']
    assert as_property_list(None) is None
    assert as_property_list([]) is None


def test_as_property_list_rejects_non_strings():
    with pytest.raises(ProcessorExecuteError):
        as_property_list([1, 2])


# ------------------------------------------------------------ clip processing

def test_clip_returns_geojson():
    processor = make_clip_processor()

    mimetype, output = processor.execute({'table': 'boreholes',
                                          'wkt': POLYGON})

    assert mimetype == 'application/json'
    assert output['type'] == 'FeatureCollection'


def test_clip_passes_inputs_through():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({
        'table': 'public.bedrock',
        'wkt': f'SRID=27700;{POLYGON}',
        'output_srid': 27700,
        'limit': 25,
        'properties': ['unit'],
        'clip': False
    })

    call = db.clip_calls[0]
    assert call['table']['name'] == 'public.bedrock'
    assert call['wkt_srid'] == 27700
    assert call['output_srid'] == 27700
    assert call['limit'] == 25
    assert call['properties'] == ['unit']
    assert call['clip_geometries'] is False


def test_clip_accepts_qualified_input_values():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({'table': {'value': 'boreholes'},
                       'wkt': {'value': POLYGON}})

    assert db.clip_calls[0]['table']['table'] == 'boreholes'


@pytest.mark.parametrize('inputs', [
    {'wkt': POLYGON},
    {'table': 'boreholes'},
    {'table': 'boreholes', 'wkt': 'POINT(0 0)'},
    {'table': 'boreholes; DROP TABLE users', 'wkt': POLYGON},
    {'table': 'boreholes', 'wkt': POLYGON, 'limit': 0},
    # two clip areas at once
    {'table': 'boreholes', 'wkt': POLYGON, 'bbox': [0, 0, 1, 1]},
    {'table': 'boreholes', 'bbox': [1, 1, 0, 0]},
    {'table': 'boreholes', 'bbox': [0, 0, 1]},
    {'table': 'boreholes', 'geometry': {'type': 'Point',
                                        'coordinates': [0, 0]}},
    {'table': 'boreholes', 'wkt': POLYGON, 'simplify': 'sort of'},
    {'table': 'boreholes', 'wkt': POLYGON, 'simplify': -1}
])
def test_clip_rejects_bad_input(inputs):
    processor = make_clip_processor()

    with pytest.raises(ProcessorExecuteError):
        processor.execute(inputs)


def test_clip_reports_unknown_table():
    processor = make_clip_processor()

    with pytest.raises(ProcessorExecuteError, match='not available'):
        processor.execute({'table': 'users', 'wkt': POLYGON})


def test_clip_wraps_database_errors():
    processor = make_clip_processor(FakeDB(error=DatabaseError('boom')))

    with pytest.raises(ProcessorExecuteError, match='boom'):
        processor.execute({'table': 'boreholes', 'wkt': POLYGON})


# ------------------------------------------------------------- list processing

def test_list_tables_returns_catalogue():
    processor = make_list_processor()

    mimetype, output = processor.execute({})

    assert mimetype == 'application/json'
    assert output['count'] == 2
    assert output['tables'][0]['name'] == 'public.boreholes'
    assert output['schemas'] == ['public']


def test_list_tables_forwards_filters():
    db = FakeDB()
    processor = make_list_processor(db)

    processor.execute({'schema': 'public', 'match': 'bore'})

    assert db.list_calls[0] == {'schema': 'public', 'match': 'bore'}


def test_list_tables_handles_no_inputs():
    processor = make_list_processor()

    assert processor.execute(None)[1]['count'] == 2


# ------------------------------------------------------- clip area inputs

GEOJSON_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-3.25, 55.92], [-3.10, 55.92], [-3.10, 56.00],
                     [-3.25, 56.00], [-3.25, 55.92]]]
}


def test_clip_accepts_a_geojson_geometry():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({'table': 'boreholes', 'geometry': GEOJSON_POLYGON})

    assert db.clip_calls[0]['wkt'].startswith('POLYGON')


def test_clip_accepts_a_feature_collection():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({
        'table': 'boreholes',
        'geometry': {
            'type': 'FeatureCollection',
            'features': [
                {'type': 'Feature', 'properties': {},
                 'geometry': GEOJSON_POLYGON},
                {'type': 'Feature', 'properties': {}, 'geometry': {
                    'type': 'Polygon',
                    'coordinates': [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
                }}
            ]
        }
    })

    # two drawn polygons become one multipolygon area of interest
    assert db.clip_calls[0]['wkt'].startswith('MULTIPOLYGON')


def test_clip_accepts_a_bbox():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({'table': 'boreholes',
                       'bbox': [-3.25, 55.92, -3.10, 56.00],
                       'srid': 4326})

    call = db.clip_calls[0]
    assert call['wkt'].startswith('POLYGON')
    assert call['wkt_srid'] == 4326


def test_clip_accepts_a_bbox_as_a_string():
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({'table': 'boreholes',
                       'bbox': '-3.25, 55.92, -3.10, 56.00'})

    assert db.clip_calls[0]['wkt'].startswith('POLYGON')


def test_clip_requires_a_clip_area():
    processor = make_clip_processor()

    with pytest.raises(ProcessorExecuteError, match='clip area is required'):
        processor.execute({'table': 'boreholes'})


@pytest.mark.parametrize('value,expected', [
    (None, None), (False, None), ('false', None),
    (True, True), ('true', True), (0.5, 0.5), ('0.5', 0.5)
])
def test_clip_passes_simplify_through(value, expected):
    db = FakeDB()
    processor = make_clip_processor(db)

    processor.execute({'table': 'boreholes', 'wkt': POLYGON,
                       'simplify': value})

    assert db.clip_calls[0]['simplify'] == expected


# --------------------------------------------------------------- formats

def test_clip_returns_geojson_by_default():
    processor = make_clip_processor()

    mimetype, output = processor.execute({'table': 'boreholes',
                                          'wkt': POLYGON})

    assert mimetype == 'application/json'
    assert isinstance(output, dict)


@pytest.mark.parametrize('fmt,expected_type', [
    ('gpkg', 'application/geopackage+sqlite3'),
    ('geopackage', 'application/geopackage+sqlite3'),
    ('fgb', 'application/flatgeobuf'),
    ('flatgeobuf', 'application/flatgeobuf')
])
def test_clip_returns_binary_formats(fmt, expected_type):
    pytest.importorskip('fiona')
    processor = make_clip_processor()

    mimetype, output = processor.execute({'table': 'boreholes',
                                          'wkt': POLYGON, 'format': fmt})

    assert mimetype == expected_type
    assert isinstance(output, bytes)
    assert output


def test_clip_rejects_an_unknown_format():
    processor = make_clip_processor()

    with pytest.raises(ProcessorExecuteError, match='unsupported format'):
        processor.execute({'table': 'boreholes', 'wkt': POLYGON,
                           'format': 'shp'})


def test_binary_output_is_labelled_with_the_output_crs(tmp_path):
    fiona = pytest.importorskip('fiona')
    processor = make_clip_processor()

    _, output = processor.execute({'table': 'boreholes', 'wkt': POLYGON,
                                   'format': 'gpkg', 'output_srid': 27700})

    path = tmp_path / 'out.gpkg'
    path.write_bytes(output)

    with fiona.open(path) as source:
        # the CRS of the file is where the geometries ended up, not where
        # the clip area came from
        assert str(source.crs) == 'EPSG:27700'


def test_binary_output_defaults_to_the_servers_output_crs(tmp_path):
    fiona = pytest.importorskip('fiona')
    processor = make_clip_processor()

    _, output = processor.execute({'table': 'boreholes',
                                   'wkt': f'SRID=27700;{POLYGON}',
                                   'format': 'gpkg'})

    path = tmp_path / 'out.gpkg'
    path.write_bytes(output)

    with fiona.open(path) as source:
        assert str(source.crs) == 'EPSG:4326'
