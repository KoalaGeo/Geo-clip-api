# =================================================================
#
# Geo clip API: list-tables process
#
# =================================================================

"""OGC API - Processes plugin listing the clippable PostGIS tables."""

from __future__ import annotations

import logging

from pygeoapi.process.base import BaseProcessor

from geoclip.errors import GeoClipError
from geoclip.processes.common import (database_from_definition, get_input,
                                      translate_error)

LOGGER = logging.getLogger(__name__)

#: Process metadata and description
PROCESS_METADATA = {
    'version': '0.1.0',
    'id': 'list-tables',
    'title': {
        'en': 'List clippable tables'
    },
    'description': {
        'en': 'Lists the spatial tables published by this server, with '
              'their geometry column, geometry type, SRID and an estimated '
              'row count. The names returned here are what the clip process '
              'accepts as its table input.'
    },
    'jobControlOptions': ['sync-execute'],
    'keywords': ['postgis', 'catalogue', 'tables', 'metadata'],
    'links': [{
        'type': 'text/html',
        'rel': 'about',
        'title': 'Geo clip API documentation',
        'href': 'https://github.com/KoalaGeo/Geo-clip-api',
        'hreflang': 'en-US'
    }],
    'inputs': {
        'schema': {
            'title': 'Schema',
            'description': 'Restrict the listing to one published schema.',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 0,
            'maxOccurs': 1
        },
        'match': {
            'title': 'Name filter',
            'description': 'Case-insensitive substring matched against the '
                           'qualified table name.',
            'schema': {
                'type': 'string'
            },
            'minOccurs': 0,
            'maxOccurs': 1
        }
    },
    'outputs': {
        'tables': {
            'title': 'Tables',
            'description': 'The spatial tables available for clipping.',
            'schema': {
                'type': 'object',
                'contentMediaType': 'application/json'
            }
        }
    },
    'example': {
        'inputs': {
            'match': 'borehole'
        }
    }
}


class ListTablesProcessor(BaseProcessor):
    """list the spatial tables that the clip process can read"""

    def __init__(self, processor_def):
        """
        Initialize object

        :param processor_def: processor definition

        :returns: geoclip.processes.list_tables.ListTablesProcessor
        """

        super().__init__(processor_def, PROCESS_METADATA)
        self.db = database_from_definition(processor_def)
        LOGGER.debug(f'list-tables process bound to {self.db.summary}')

    def execute(self, data, outputs=None):
        """
        list the published spatial tables

        :param data: `dict` of process inputs
        :param outputs: unused; the process has a single output

        :returns: `tuple` of MIME type and `dict` of tables
        """

        schema = get_input(data or {}, 'schema')
        match = get_input(data or {}, 'match')

        try:
            tables = self.db.list_tables(schema=schema, match=match)
        except GeoClipError as err:
            raise translate_error(err)

        LOGGER.debug(f'{len(tables)} tables available')

        return 'application/json', {
            'tables': tables,
            'count': len(tables),
            'schemas': self.db.allowed_schemas
        }

    def __repr__(self):
        return f'<ListTablesProcessor> {self.name}'
