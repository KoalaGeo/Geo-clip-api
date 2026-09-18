# =================================================================
#
# Geo clip API: output formats
#
# =================================================================

"""Turn a GeoJSON FeatureCollection into a GeoPackage or FlatGeobuf.

The clip query always builds GeoJSON in PostGIS; these writers take that
collection and hand back file bytes, so the extra formats cost one OGR
write rather than a second code path through the database.

OGR is reached through fiona, which the pygeoapi base image already ships
as a system package. It is imported lazily so that a server without it
still serves GeoJSON and reports a clear error for the other formats.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from geoclip.errors import GeoClipError, InvalidInputError

LOGGER = logging.getLogger(__name__)

#: name -> (OGR driver, file extension, media type). GeoJSON is written by
#: PostgreSQL itself, hence the empty driver
FORMATS = {
    'geojson': (None, 'geojson', 'application/geo+json'),
    'gpkg': ('GPKG', 'gpkg', 'application/geopackage+sqlite3'),
    'fgb': ('FlatGeobuf', 'fgb', 'application/flatgeobuf')
}

#: what people actually type
ALIASES = {
    'json': 'geojson',
    'geo+json': 'geojson',
    'application/geo+json': 'geojson',
    'geopackage': 'gpkg',
    'geo package': 'gpkg',
    'flatgeobuf': 'fgb',
    'flat geobuf': 'fgb'
}

DEFAULT_FORMAT = 'geojson'
DEFAULT_LAYER_NAME = 'clip'

#: python type -> fiona field type. fiona has no boolean, so booleans are
#: written as 0/1 integers
FIELD_TYPES = {
    bool: 'int',
    int: 'int',
    float: 'float',
    str: 'str'
}

#: widening rules when a column holds more than one type
FIELD_PRECEDENCE = ('int', 'float', 'str')


class FormatError(GeoClipError):
    """the requested format cannot be written"""


def parse_format(value: Any) -> str:
    """
    validate the requested output format

    :param value: format name, or ``None`` for the default

    :returns: `str` key of `FORMATS`

    :raises: `geoclip.errors.InvalidInputError`
    """

    if value in (None, ''):
        return DEFAULT_FORMAT

    if not isinstance(value, str):
        raise InvalidInputError(f'format must be a string, got {value!r}')

    name = value.strip().lower()
    name = ALIASES.get(name, name)

    if name not in FORMATS:
        supported = ', '.join(sorted(FORMATS))
        raise InvalidInputError(
            f'unsupported format {value!r}; supported formats: {supported}')

    return name


def media_type(fmt: str) -> str:
    """
    media type of an output format

    :param fmt: format key

    :returns: `str` media type
    """

    return FORMATS[fmt][2]


def extension(fmt: str) -> str:
    """
    file extension of an output format

    :param fmt: format key

    :returns: `str` extension
    """

    return FORMATS[fmt][1]


def layer_name(name: Optional[str]) -> str:
    """
    turn a table name into a layer name OGR is happy with

    :param name: source table name, qualified or not

    :returns: `str` layer name
    """

    if not name:
        return DEFAULT_LAYER_NAME

    candidate = re.sub(r'[^A-Za-z0-9_]', '_', name.split('.')[-1])

    return candidate or DEFAULT_LAYER_NAME


def build_schema(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    infer an OGR schema from the features of a collection

    Geometries are written as a mixed layer: clipping a multipolygon table
    routinely yields both polygons and multipolygons.

    :param features: GeoJSON features

    :returns: `dict` fiona schema
    """

    properties: Dict[str, str] = {}

    for feature in features:
        for name, value in (feature.get('properties') or {}).items():
            if value is None:
                # remember the column, decide its type from another row
                properties.setdefault(name, None)
                continue

            field_type = FIELD_TYPES.get(type(value), 'str')
            current = properties.get(name)

            if current is None:
                properties[name] = field_type
            elif current != field_type:
                # keep the type that can hold both
                properties[name] = max(
                    (current, field_type), key=FIELD_PRECEDENCE.index)

    return {
        'geometry': 'Unknown',
        'properties': {name: kind or 'str'
                       for name, kind in properties.items()}
    }


def write_collection(collection: Dict[str, Any], fmt: str,
                     srid: Optional[int] = 4326,
                     name: Optional[str] = None) -> bytes:
    """
    write a GeoJSON FeatureCollection as GeoPackage or FlatGeobuf

    :param collection: GeoJSON FeatureCollection
    :param fmt: format key, as returned by `parse_format`
    :param srid: EPSG code of the geometries, or ``None`` to write the file
                 without a CRS (a source table with SRID 0)
    :param name: source table name, used to name the layer

    :returns: `bytes` of the written file

    :raises: `geoclip.formats.FormatError`
    """

    driver, suffix, _ = FORMATS[fmt]

    if driver is None:
        raise FormatError(f'{fmt} is not written through OGR')

    try:
        import fiona
    except ImportError:
        raise FormatError(
            f'the {fmt} format needs fiona (python3-fiona), which is not '
            'installed on this server')

    features = collection.get('features') or []
    schema = build_schema(features)
    # properties not in the schema would be silently dropped; the schema is
    # built from the same features, so this only guards against surprises
    fields = list(schema['properties'])

    with tempfile.TemporaryDirectory(prefix='geoclip-') as directory:
        path = Path(directory) / f'{layer_name(name)}.{suffix}'

        try:
            with fiona.open(path, 'w', driver=driver, schema=schema,
                            crs=f'EPSG:{srid}' if srid else None,
                            layer=layer_name(name)) as sink:
                sink.writerecords([{
                    'type': 'Feature',
                    'geometry': feature.get('geometry'),
                    'properties': {
                        field: _field_value(
                            (feature.get('properties') or {}).get(field))
                        for field in fields
                    }
                } for feature in features])
        except Exception as err:
            LOGGER.exception(err)
            raise FormatError(f'could not write {fmt}: {err}')

        return path.read_bytes()


def _field_value(value: Any) -> Any:
    """
    coerce a property value to something OGR can store

    :param value: property value

    :returns: the value, as `int` for booleans and `str` for anything
              structured
    """

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, (dict, list)):
        return str(value)

    return value
