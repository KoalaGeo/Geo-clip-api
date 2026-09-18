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

from geoclip.errors import GeoClipError
from geoclip.geometry import parse_clip_geometry
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
                           'overrides the srid input.',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 1,
            'maxOccurs': 1,
            'keywords': ['wkt', 'polygon', 'area of interest']
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
            'description': 'GeoJSON FeatureCollection of the clipped data.',
            'schema': {
                'type': 'object',
                'contentMediaType': 'application/geo+json'
            }
        }
    },
    'example': {
        'inputs': {
            'table': 'public.boreholes',
            'wkt': EXAMPLE_WKT,
            'srid': 4326,
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
        wkt_input = get_input(data, 'wkt')

        if not table_name:
            raise ClipInputError('table is a required input',
                                 user_msg='table is a required input')

        if not wkt_input:
            raise ClipInputError('wkt is a required input',
                                 user_msg='wkt is a required input')

        wkt, srid = self._parse_geometry(wkt_input, get_input(data, 'srid',
                                                              4326))
        clip_geometries = as_bool(get_input(data, 'clip'), 'clip')
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
                clip_geometries=clip_geometries)
        except GeoClipError as err:
            raise translate_error(err)

        elapsed = time.monotonic() - start
        LOGGER.info(
            f'clipped {collection["numberReturned"]} features from '
            f'{table["name"]} in {elapsed:.3f}s')

        return 'application/json', collection

    def _parse_geometry(self, wkt_input, srid):
        """
        validate the clip geometry, translating errors for pygeoapi

        :param wkt_input: WKT or EWKT string
        :param srid: SRID to assume when no EWKT prefix is present

        :returns: `tuple` of (wkt, srid)
        """

        try:
            return parse_clip_geometry(wkt_input, srid)
        except GeoClipError as err:
            raise translate_error(err)

    def __repr__(self):
        return f'<ClipProcessor> {self.name}'
