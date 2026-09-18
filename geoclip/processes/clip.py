# =================================================================
#
# Geo clip API: clip process
#
# =================================================================

"""OGC API - Processes plugin clipping a PostGIS table to a WKT geometry."""

from __future__ import annotations

import logging
import time

from pygeoapi.process.base import BaseProcessor

from geoclip.db import parse_simplify
from geoclip.errors import GeoClipError
from geoclip.formats import (DEFAULT_FORMAT, FORMATS, media_type,
                             parse_format, write_collection)
from geoclip.geometry import clip_area
from geoclip.processes.common import (ClipInputError, database_from_definition,
                                      get_input, translate_error)

LOGGER = logging.getLogger(__name__)

EXAMPLE_WKT = ('POLYGON((-3.20 55.94, -3.15 55.94, -3.15 55.97, '
               '-3.20 55.97, -3.20 55.94))')

#: Process metadata and description
PROCESS_METADATA = {
    'version': '0.1.0',
    'id': 'clip',
    'title': {
        'en': 'Clip a PostGIS table to a WKT geometry'
    },
    'description': {
        'en': 'Clips the features of a PostGIS table to a polygon or '
              'multipolygon supplied as WKT, and returns the result as a '
              'GeoJSON FeatureCollection. Use the list-tables process to '
              'discover which tables are available.'
    },
    'jobControlOptions': ['sync-execute', 'async-execute'],
    'keywords': ['clip', 'postgis', 'wkt', 'geojson', 'intersection'],
    'links': [{
        'type': 'text/html',
        'rel': 'about',
        'title': 'Geo clip API documentation',
        'href': 'https://github.com/KoalaGeo/Geo-clip-api',
        'hreflang': 'en-US'
    }],
    'inputs': {
        'wkt': {
            'title': 'Clip geometry (WKT)',
            'description': 'POLYGON or MULTIPOLYGON in WKT. An EWKT prefix '
                           '("SRID=27700;POLYGON((...))") sets the CRS and '
                           'overrides the srid input. One of wkt, geometry '
                           'or bbox is required.',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 0,
            'maxOccurs': 1,
            'keywords': ['wkt', 'polygon', 'area of interest']
        },
        'geometry': {
            'title': 'Clip geometry (GeoJSON)',
            'description': 'A GeoJSON Polygon or MultiPolygon, or a Feature '
                           'or FeatureCollection carrying them - what a '
                           'Leaflet, OpenLayers or MapLibre drawing control '
                           'produces. Several features are merged into one '
                           'area. One of wkt, geometry or bbox is required.',
            'schema': {
                'type': 'object',
                'contentMediaType': 'application/geo+json'
            },
            'minOccurs': 0,
            'maxOccurs': 1,
            'keywords': ['geojson', 'polygon', 'area of interest']
        },
        'bbox': {
            'title': 'Clip bounding box',
            'description': 'Area of interest as [minx, miny, maxx, maxy] in '
                           'the CRS given by srid, for clipping to a map '
                           'viewport. One of wkt, geometry or bbox is '
                           'required.',
            'schema': {
                'type': 'array',
                'items': {'type': 'number'},
                'minItems': 4,
                'maxItems': 4
            },
            'minOccurs': 0,
            'maxOccurs': 1,
            'keywords': ['bbox', 'extent', 'viewport']
        },
        'table': {
            'title': 'Table',
            'description': 'Table to clip, as "table" or "schema.table".',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 1,
            'maxOccurs': 1,
            'keywords': ['table', 'layer']
        },
        'geometry_column': {
            'title': 'Geometry column',
            'description': 'Geometry column to clip on. Only required for '
                           'tables carrying more than one geometry column.',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'srid': {
            'title': 'Clip geometry SRID',
            'description': 'EPSG code of the WKT clip geometry (default '
                           '4326). Ignored when EWKT is supplied.',
            'schema': {
                'type': 'integer',
                'default': 4326
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'output_srid': {
            'title': 'Output SRID',
            'description': 'EPSG code of the returned geometries (default '
                           '4326, as required by GeoJSON).',
            'schema': {
                'type': 'integer',
                'default': 4326
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'properties': {
            'title': 'Properties',
            'description': 'Subset of columns to return as feature '
                           'properties. All non-geometry columns by default.',
            'schema': {
                'type': 'array',
                'items': {'type': 'string'}
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'limit': {
            'title': 'Feature limit',
            'description': 'Maximum number of features to return, capped by '
                           'the server max_features setting.',
            'schema': {
                'type': 'integer',
                'minimum': 1
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'simplify': {
            'title': 'Simplify output geometries',
            'description': 'true simplifies the returned geometries with '
                           'ST_SimplifyPreserveTopology, using a tolerance '
                           'of about one pixel of the clip extent on a 2000 '
                           'pixel wide map. A number sets the tolerance '
                           'explicitly, in output CRS units.',
            'schema': {
                'oneOf': [
                    {'type': 'boolean', 'default': False},
                    {'type': 'number', 'exclusiveMinimum': 0}
                ]
            },
            'minOccurs': 0,
            'maxOccurs': 1,
            'keywords': ['simplify', 'generalise', 'web map']
        },
        'format': {
            'title': 'Output format',
            'description': 'geojson (default) returns a FeatureCollection; '
                           'gpkg returns a GeoPackage and fgb a FlatGeobuf, '
                           'both as binary file downloads carrying the '
                           'clipped features in one layer.',
            'schema': {
                'type': 'string',
                'enum': sorted(FORMATS),
                'default': DEFAULT_FORMAT
            },
            'minOccurs': 0,
            'maxOccurs': 1,
            'keywords': ['format', 'geopackage', 'flatgeobuf', 'download']
        },
        'clip': {
            'title': 'Clip geometries',
            'description': 'True (default) cuts geometries at the boundary '
                           'of the clip geometry; false returns intersecting '
                           'features whole.',
            'schema': {
                'type': 'boolean',
                'default': True
            },
            'minOccurs': 0,
            'maxOccurs': 1
        }
    },
    'outputs': {
        'featureCollection': {
            'title': 'Clipped features',
            'description': 'The clipped data: a GeoJSON FeatureCollection, '
                           'or a GeoPackage or FlatGeobuf file when the '
                           'format input asks for one.',
            'schema': {
                'oneOf': [
                    {'type': 'object',
                     'contentMediaType': 'application/geo+json'},
                    {'type': 'string',
                     'contentMediaType': 'application/geopackage+sqlite3',
                     'contentEncoding': 'binary'},
                    {'type': 'string',
                     'contentMediaType': 'application/flatgeobuf',
                     'contentEncoding': 'binary'}
                ]
            }
        }
    },
    'example': {
        'inputs': {
            'table': 'public.boreholes',
            'bbox': [-3.20, 55.94, -3.15, 55.97],
            'srid': 4326,
            'simplify': True,
            'limit': 1000
        }
    }
}

TRUTHY = ('true', 't', 'yes', 'y', '1')
FALSY = ('false', 'f', 'no', 'n', '0')


def as_bool(value, name: str, default: bool = True) -> bool:
    """
    coerce a process input to a boolean

    :param value: raw input value
    :param name: input name, for error messages
    :param default: value to use when the input is absent

    :returns: `bool`
    """

    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        if value.strip().lower() in TRUTHY:
            return True
        if value.strip().lower() in FALSY:
            return False

    raise ClipInputError(f'{name} must be a boolean, got {value!r}',
                         user_msg=f'{name} must be a boolean, got {value!r}')


def as_positive_int(value, name: str):
    """
    coerce a process input to a positive integer, or ``None``

    :param value: raw input value
    :param name: input name, for error messages

    :returns: `int` or ``None``
    """

    if value is None or value == '':
        return None

    try:
        limit = int(value)
    except (TypeError, ValueError):
        msg = f'{name} must be an integer, got {value!r}'
        raise ClipInputError(msg, user_msg=msg)

    if limit < 1:
        msg = f'{name} must be greater than zero'
        raise ClipInputError(msg, user_msg=msg)

    return limit


def as_property_list(value, name: str = 'properties'):
    """
    coerce a process input to a list of column names

    :param value: raw input value (list, or comma separated string)
    :param name: input name, for error messages

    :returns: `list` of `str`, or ``None``
    """

    if value is None:
        return None

    if isinstance(value, str):
        value = [p.strip() for p in value.split(',') if p.strip()]

    if not isinstance(value, list) or not all(
            isinstance(p, str) for p in value):
        msg = f'{name} must be an array of column names'
        raise ClipInputError(msg, user_msg=msg)

    return value or None


class ClipProcessor(BaseProcessor):
    """clip a PostGIS table to a WKT polygon"""

    def __init__(self, processor_def):
        """
        Initialize object

        :param processor_def: processor definition

        :returns: geoclip.processes.clip.ClipProcessor
        """

        super().__init__(processor_def, PROCESS_METADATA)
        self.db = database_from_definition(processor_def)
        LOGGER.debug(f'clip process bound to {self.db.summary}')

    def execute(self, data, outputs=None):
        """
        execute the clip

        :param data: `dict` of process inputs
        :param outputs: unused; the process has a single output

        :returns: `tuple` of MIME type and GeoJSON FeatureCollection
        """

        table_name = get_input(data, 'table')

        if not table_name:
            raise ClipInputError('table is a required input',
                                 user_msg='table is a required input')

        wkt, srid = self._clip_area(data)
        clip_geometries = as_bool(get_input(data, 'clip'), 'clip')
        simplify = self._simplify(get_input(data, 'simplify'))
        output_format = self._format(get_input(data, 'format'))
        properties = as_property_list(get_input(data, 'properties'))
        output_srid = as_positive_int(get_input(data, 'output_srid'),
                                      'output_srid')
        limit = as_positive_int(get_input(data, 'limit'), 'limit')

        start = time.monotonic()

        try:
            table = self.db.get_table(
                table_name, geometry_column=get_input(data,
                                                      'geometry_column'))
            collection = self.db.clip(
                table, wkt, wkt_srid=srid, output_srid=output_srid,
                limit=limit, properties=properties,
                clip_geometries=clip_geometries, simplify=simplify)
        except GeoClipError as err:
            raise translate_error(err)

        elapsed = time.monotonic() - start
        LOGGER.info(
            f'clipped {collection["numberReturned"]} features from '
            f'{table["name"]} in {elapsed:.3f}s')

        if output_format == DEFAULT_FORMAT:
            return 'application/json', collection

        # the file is labelled with the CRS the geometries are actually in,
        # which is the output CRS, not the CRS the clip area came in
        written_srid = None
        if table['srid']:
            written_srid = output_srid or self.db.default_output_srid

        return self._encode(collection, output_format, table, written_srid)

    def _clip_area(self, data):
        """
        resolve the clip area from the wkt, geometry and bbox inputs

        :param data: `dict` of process inputs

        :returns: `tuple` of (wkt, srid)
        """

        try:
            return clip_area(wkt=get_input(data, 'wkt'),
                             geometry=get_input(data, 'geometry'),
                             bbox=get_input(data, 'bbox'),
                             srid=get_input(data, 'srid', 4326))
        except GeoClipError as err:
            raise translate_error(err)

    def _format(self, value):
        """
        validate the requested output format

        :param value: format name

        :returns: `str` format key
        """

        try:
            return parse_format(value)
        except GeoClipError as err:
            raise translate_error(err)

    def _encode(self, collection, output_format, table, srid):
        """
        write the collection as a binary format

        :param collection: GeoJSON FeatureCollection
        :param output_format: format key
        :param table: table description, used to name the layer
        :param srid: EPSG code of the geometries, or ``None`` when the
                     source table has no known CRS

        :returns: `tuple` of media type and file `bytes`
        """

        try:
            data = write_collection(collection, output_format,
                                    int(srid) if srid else None,
                                    table.get('name'))
        except GeoClipError as err:
            raise translate_error(err)

        LOGGER.info(f'wrote {len(data)} bytes of {output_format}')

        return media_type(output_format), data

    def _simplify(self, value):
        """
        validate the simplify input before touching the database

        :param value: ``True``/``False``, or a tolerance

        :returns: ``True``, ``None``, or a `float` tolerance
        """

        try:
            return parse_simplify(value)
        except GeoClipError as err:
            raise translate_error(err)

    def __repr__(self):
        return f'<ClipProcessor> {self.name}'
