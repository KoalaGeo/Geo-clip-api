# =================================================================
#
# Geo clip API: image self-check
#
# Run at build time. Importing fiona is not enough: the GDAL inside its
# wheel links system libraries, and a missing one only shows up when a
# driver actually runs. So write both binary formats for real.
#
# =================================================================

import fiona
import psycopg2  # noqa: F401  (checked for import only)
import shapely  # noqa: F401

from geoclip.api import app  # noqa: F401
from geoclip.formats import write_collection

COLLECTION = {
    'type': 'FeatureCollection',
    'features': [{
        'type': 'Feature',
        'geometry': {'type': 'Point', 'coordinates': [-3.19, 55.95]},
        'properties': {'name': 'check'}
    }]
}

for fmt in ('gpkg', 'fgb'):
    written = write_collection(COLLECTION, fmt, 4326, 'check')

    if not written:
        raise SystemExit(f'{fmt} wrote nothing')

    print(f'  {fmt}: {len(written)} bytes')

print(f'fiona {fiona.__version__}, GDAL {fiona.__gdal_version__}: '
      'both formats write')
