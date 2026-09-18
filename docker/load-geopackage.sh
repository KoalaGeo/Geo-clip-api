#!/bin/sh
# =================================================================
#
# Geo clip API: load a GeoPackage into PostGIS for testing
#
# Runs in the GDAL alpine image as a docker compose stage: every spatial
# layer of the GeoPackage is pushed into PostGIS with ogr2ogr, so the clip
# and list-tables processes have real data to work against.
#
# Layers already present are skipped, which keeps `docker compose up`
# cheap after the first run; set GPKG_FORCE=true to reload them.
#
# =================================================================

set -e

GPKG_PATH="${GPKG_PATH:-/data/625k_V5_Geology_UK_EPSG27700.gpkg}"
GPKG_SCHEMA="${GPKG_SCHEMA:-public}"
GPKG_FORCE="${GPKG_FORCE:-false}"
#: assigned to every layer with -a_srs (no reprojection). The geology
#: GeoPackage stores three of its layers against a private SRS id (100000)
#: that GDAL cannot map to an EPSG code, which would land them in PostGIS
#: with an unknown SRID and stop the clip process reprojecting them.
GPKG_SRS="${GPKG_SRS:-}"

PG_CONNECTION="host=${POSTGRES_HOST:-postgres} port=${POSTGRES_PORT:-5432} \
dbname=${POSTGRES_DB:-geodata} user=${POSTGRES_USER:-postgres} \
password=${POSTGRES_PASSWORD:-postgres}"

if [ ! -f "${GPKG_PATH}" ]; then
    echo "geopackage ${GPKG_PATH} not found; nothing to load"
    exit 0
fi

# PostgreSQL folds unquoted names to lower case and ogr2ogr launders layer
# names the same way; compute the target name so it can be reported and
# checked for beforehand
target_table() {
    echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_]/_/g'
}

# spatial layers only: ogrinfo prints a geometry type in brackets for those,
# and nothing for attribute tables such as QGIS' layer_styles
spatial_layers() {
    ogrinfo -q "${GPKG_PATH}" | sed -n 's/^[0-9]*: \(.*\) (.*)$/\1/p'
}

layer_exists() {
    ogrinfo -so "PG:${PG_CONNECTION}" "${GPKG_SCHEMA}.$1" > /dev/null 2>&1
}

LAYERS="${GPKG_LAYERS:-$(spatial_layers)}"

if [ -z "${LAYERS}" ]; then
    echo "no spatial layers found in ${GPKG_PATH}"
    exit 0
fi

echo "loading ${GPKG_PATH} into ${POSTGRES_DB:-geodata}.${GPKG_SCHEMA}"

echo "${LAYERS}" | while IFS= read -r layer; do
    [ -z "${layer}" ] && continue

    table=$(target_table "${layer}")

    if [ "${GPKG_FORCE}" != "true" ] && layer_exists "${table}"; then
        echo "  ${GPKG_SCHEMA}.${table} already loaded, skipping"
        continue
    fi

    echo "  ${layer} -> ${GPKG_SCHEMA}.${table}"

    # shellcheck disable=SC2086 # GPKG_SRS is an intentional word split
    ogr2ogr \
        -f PostgreSQL "PG:${PG_CONNECTION}" \
        "${GPKG_PATH}" "${layer}" \
        -nln "${table}" \
        -nlt PROMOTE_TO_MULTI \
        -lco SCHEMA="${GPKG_SCHEMA}" \
        -lco GEOMETRY_NAME=geom \
        -lco FID=id \
        -lco SPATIAL_INDEX=GIST \
        -lco LAUNDER=YES \
        ${GPKG_SRS:+-a_srs "${GPKG_SRS}"} \
        -overwrite \
        --config PG_USE_COPY YES
done

echo "geopackage load complete; the tables are listed by the list-tables process"
