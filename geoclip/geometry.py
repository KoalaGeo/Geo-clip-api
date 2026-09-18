# =================================================================
#
# Geo clip API: clip geometry parsing
#
# =================================================================

"""Parsing and validation of the WKT clip geometry."""

from __future__ import annotations

import logging
import re
from typing import Tuple

from shapely import wkt as shapely_wkt
from shapely.errors import ShapelyError

from geoclip.errors import InvalidInputError

LOGGER = logging.getLogger(__name__)

#: "SRID=27700;POLYGON((...))" style prefix (PostGIS EWKT)
EWKT_RE = re.compile(
    r'^\s*SRID\s*=\s*(?P<srid>\d+)\s*;\s*(?P<wkt>.+)$', re.IGNORECASE | re.S)

#: geometry types we are willing to clip with
SUPPORTED_TYPES = ('Polygon', 'MultiPolygon')


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

    if not geometry.is_valid:
        # ST_MakeValid in the clip query repairs self-intersections etc.,
        # so this is a warning rather than a rejection
        LOGGER.warning('clip geometry is not OGC valid; repairing with '
                       'ST_MakeValid')

    return wkt, srid
