# =================================================================
#
# Geo clip API: pygeoapi server with the geoclip process plugins
#
# The geopython/pygeoapi image already ships psycopg2 and shapely as
# system packages, and runs pygeoapi from the virtualenv in /venv, so the
# plugins are installed there with --no-deps.
#
# =================================================================

FROM geopython/pygeoapi:latest

LABEL org.opencontainers.image.title="geo-clip-api"
LABEL org.opencontainers.image.description="pygeoapi with PostGIS WKT clipping processes"
LABEL org.opencontainers.image.source="https://github.com/KoalaGeo/Geo-clip-api"

# plugin source
COPY pyproject.toml README.md /geo-clip-api/
COPY geoclip /geo-clip-api/geoclip

# install into the image's virtualenv; psycopg2 and shapely come from the
# base image's system site-packages
RUN /venv/bin/python3 -m pip install --no-cache-dir --no-deps /geo-clip-api \
    && /venv/bin/python3 -c "\
from geoclip.processes.clip import ClipProcessor; \
from geoclip.processes.list_tables import ListTablesProcessor; \
print('geoclip plugins importable')" \
    && { /venv/bin/python3 -c "\
import fiona; print('fiona', fiona.__version__, 'GDAL', fiona.__gdal_version__)" \
       || echo 'WARNING: fiona missing, gpkg and fgb output will be refused'; }

# pygeoapi reads /pygeoapi/local.config.yml unless PYGEOAPI_CONFIG says
# otherwise; mount your own over this one to change published tables
COPY pygeoapi-config.yml /pygeoapi/local.config.yml

# the process manager writes job results to output_dir and does not create
# it, so the entrypoint below makes both manager paths before starting
COPY docker/entrypoint.sh /geoclip-entrypoint.sh
# the CR stripping guards against a CRLF checkout on Windows, which would
# otherwise make /bin/bash fail to read the script
RUN sed -i 's/\r$//' /geoclip-entrypoint.sh \
    && chmod +x /geoclip-entrypoint.sh \
    && mkdir -p /tmp/pygeoapi-process-outputs

ENV PYGEOAPI_CONFIG=/pygeoapi/local.config.yml \
    PYGEOAPI_OPENAPI=/pygeoapi/local.openapi.yml \
    GEOCLIP_PROCESS_OUTPUT_DIR=/tmp/pygeoapi-process-outputs \
    GEOCLIP_JOB_DB=/tmp/pygeoapi-process-manager.db

ENTRYPOINT ["/geoclip-entrypoint.sh"]

EXPOSE 80
