# =================================================================
#
# Geo clip API: clip geometry parsing
#
# =================================================================

"""Parsing and validation of the clip geometry.

The clip area can arrive three ways, which all end up as WKT for the
database layer:

* ``wkt`` - WKT or EWKT, for humans, QGIS and curl
* ``geometry`` - a GeoJSON geometry, Feature or FeatureCollection, which is
  what Leaflet, OpenLayers and MapLibre hand you
* ``bbox`` - ``[minx, miny, maxx, maxy]``, the "clip to what I am looking
  at" case
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Optional, Tuple

from shapely import wkt as shapely_wkt
from shapely.errors import ShapelyError
from shapely.geometry import box, shape
from shapely.ops import unary_union

from geoclip.errors import InvalidInputError

LOGGER = logging.getLogger(__name__)

#: "SRID=27700;POLYGON((...))" style prefix (PostGIS EWKT)
EWKT_RE = re.compile(
    r'^\s*SRID\s*=\s*(?P<srid>\d+)\s*;\s*(?P<wkt>.+)$', re.IGNORECASE | re.S)

#: geometry types we are willing to clip with
SUPPORTED_TYPES = ('Polygon', 'MultiPolygon')

#: the inputs that can carry a clip area; exactly one must be given
CLIP_INPUTS = ('wkt', 'geometry', 'bbox')


def parse_clip_geometry(value: str, srid: int = 4326) -> Tuple[str, int]:
    """
    validate the user supplied clip geometry

    Accepts WKT or EWKT; an ``SRID=<n>;`` prefix wins over the ``srid``
    argument.  The WKT itself is returned untouched so that PostGIS parses
    exactly what the caller sent (no precision is lost in round-tripping).

    :param value: WKT or EWKT string
    :param srid: SRID to assume when the string carries no ``SRID=`` prefix

    :returns: `tuple` of (wkt, srid)

    :raises: `geoclip.errors.InvalidInputError`
    """

    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError('a WKT clip geometry is required')

    wkt = value.strip()

    match = EWKT_RE.match(wkt)
    if match:
        srid = int(match.group('srid'))
        wkt = match.group('wkt').strip()

    try:
        srid = int(srid)
    except (TypeError, ValueError):
        raise InvalidInputError(f'srid must be an integer, got {srid!r}')

    if srid < 0:
        raise InvalidInputError(f'srid must be positive, got {srid}')

    try:
        geometry = shapely_wkt.loads(wkt)
    except (ShapelyError, ValueError, TypeError) as err:
        raise InvalidInputError(f'could not parse WKT: {err}')

    if geometry.geom_type not in SUPPORTED_TYPES:
        raise InvalidInputError(
            f'clip geometry must be a POLYGON or MULTIPOLYGON, got '
            f'{geometry.geom_type.upper()}')

    if geometry.is_empty:
        raise InvalidInputError('clip geometry is empty')

    _check_geometry(geometry)

    return wkt, srid


def _check_geometry(geometry) -> None:
    """
    check a parsed geometry is something we can clip with

    :param geometry: shapely geometry

    :returns: `None`

    :raises: `geoclip.errors.InvalidInputError`
    """

    if geometry.geom_type not in SUPPORTED_TYPES:
        raise InvalidInputError(
            f'clip geometry must be a POLYGON or MULTIPOLYGON, got '
            f'{geometry.geom_type.upper()}')

    if geometry.is_empty:
        raise InvalidInputError('clip geometry is empty')

    if not geometry.is_valid:
        # ST_MakeValid in the clip query repairs self-intersections etc.,
        # so this is a warning rather than a rejection
        LOGGER.warning('clip geometry is not OGC valid; repairing with '
                       'ST_MakeValid')


def parse_geojson(value: Any, srid: int = 4326) -> Tuple[str, int]:
    """
    validate a GeoJSON clip geometry

    Accepts a geometry, a Feature or a FeatureCollection (whose features are
    merged into one area), as a `dict` or as a JSON string. This is what a
    Leaflet, OpenLayers or MapLibre drawing control produces.

    :param value: GeoJSON geometry, Feature or FeatureCollection
    :param srid: SRID of the coordinates (GeoJSON is WGS84 by default)

    :returns: `tuple` of (wkt, srid)

    :raises: `geoclip.errors.InvalidInputError`
    """

    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError as err:
            raise InvalidInputError(f'could not parse GeoJSON: {err}')

    if not isinstance(value, dict):
        raise InvalidInputError(
            'geometry must be a GeoJSON geometry, Feature or '
            f'FeatureCollection, got {type(value).__name__}')

    geometry = _geojson_to_shape(value)

    try:
        srid = int(srid)
    except (TypeError, ValueError):
        raise InvalidInputError(f'srid must be an integer, got {srid!r}')

    _check_geometry(geometry)

    return geometry.wkt, srid


def _geojson_to_shape(value: dict):
    """
    turn a GeoJSON object into a single shapely geometry

    :param value: GeoJSON geometry, Feature or FeatureCollection

    :returns: shapely geometry
    """

    kind = value.get('type')

    if kind == 'FeatureCollection':
        features = value.get('features')
        if not isinstance(features, list) or not features:
            raise InvalidInputError('FeatureCollection has no features')

        parts = [_geojson_to_shape(feature) for feature in features]

        # several drawn polygons are one area of interest
        merged = unary_union(parts)

        if merged.is_empty:
            raise InvalidInputError('clip geometry is empty')

        return merged

    if kind == 'Feature':
        geometry = value.get('geometry')
        if geometry is None:
            raise InvalidInputError('Feature has no geometry')
        return _geojson_to_shape(geometry)

    if kind is None:
        raise InvalidInputError(
            'GeoJSON object has no "type" member')

    try:
        geometry = shape(value)
    except (ShapelyError, AttributeError, KeyError, TypeError,
            ValueError) as err:
        raise InvalidInputError(f'could not parse GeoJSON geometry: {err}')

    if geometry.geom_type not in SUPPORTED_TYPES:
        raise InvalidInputError(
            f'clip geometry must be a POLYGON or MULTIPOLYGON, got '
            f'{geometry.geom_type.upper()}')

    return geometry


def parse_bbox(value: Any, srid: int = 4326) -> Tuple[str, int]:
    """
    turn a bounding box into a clip polygon

    :param value: ``[minx, miny, maxx, maxy]`` as a list or a comma
                  separated string
    :param srid: SRID of the coordinates

    :returns: `tuple` of (wkt, srid)

    :raises: `geoclip.errors.InvalidInputError`
    """

    if isinstance(value, str):
        value = [item.strip() for item in value.split(',') if item.strip()]

    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise InvalidInputError(
            'bbox must be [minx, miny, maxx, maxy], got '
            f'{value!r}')

    try:
        minx, miny, maxx, maxy = (float(item) for item in value)
    except (TypeError, ValueError):
        raise InvalidInputError(f'bbox must be four numbers, got {value!r}')

    if not all(math.isfinite(item) for item in (minx, miny, maxx, maxy)):
        raise InvalidInputError(f'bbox must be four finite numbers, got '
                                f'{value!r}')

    if minx >= maxx or miny >= maxy:
        raise InvalidInputError(
            'bbox must be [minx, miny, maxx, maxy] with minx < maxx and '
            f'miny < maxy, got {value!r}')

    try:
        srid = int(srid)
    except (TypeError, ValueError):
        raise InvalidInputError(f'srid must be an integer, got {srid!r}')

    return box(minx, miny, maxx, maxy).wkt, srid


def clip_area(wkt: Optional[str] = None, geometry: Any = None,
              bbox: Any = None, srid: int = 4326) -> Tuple[str, int]:
    """
    resolve the clip area from the three mutually exclusive inputs

    :param wkt: WKT or EWKT string
    :param geometry: GeoJSON geometry, Feature or FeatureCollection
    :param bbox: ``[minx, miny, maxx, maxy]``
    :param srid: SRID to assume for whichever was given

    :returns: `tuple` of (wkt, srid)

    :raises: `geoclip.errors.InvalidInputError`
    """

    given = [name for name, value in
             zip(CLIP_INPUTS, (wkt, geometry, bbox)) if value not in
             (None, '')]

    if not given:
        raise InvalidInputError(
            'a clip area is required: give one of wkt, geometry or bbox')

    if len(given) > 1:
        raise InvalidInputError(
            f'give only one clip area, got {" and ".join(given)}')

    if wkt not in (None, ''):
        return parse_clip_geometry(wkt, srid)

    if geometry not in (None, ''):
        return parse_geojson(geometry, srid)

    return parse_bbox(bbox, srid)
