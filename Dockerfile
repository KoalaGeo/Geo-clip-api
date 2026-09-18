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
print('geoclip plugins importable')"

# pygeoapi reads /pygeoapi/local.config.yml unless PYGEOAPI_CONFIG says
# otherwise; mount your own over this one to change published tables
COPY pygeoapi-config.yml /pygeoapi/local.config.yml

ENV PYGEOAPI_CONFIG=/pygeoapi/local.config.yml \
    PYGEOAPI_OPENAPI=/pygeoapi/local.openapi.yml

EXPOSE 80
