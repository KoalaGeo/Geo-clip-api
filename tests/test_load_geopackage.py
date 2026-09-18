# =================================================================
#
# Geo clip API: GeoPackage loader script tests
#
# =================================================================

"""Tests for docker/load-geopackage.sh.

ogr2ogr and ogrinfo are replaced with stubs on PATH, so the script's
decisions are checked without GDAL or a database: which layers it picks,
what it names the target tables, and which arguments ogr2ogr receives.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'docker' / \
    'load-geopackage.sh'

# ogrinfo lists the geometry type in brackets, "None" for attribute tables
# such as QGIS' layer_styles, and marks driver-internal tables private
OGRINFO_OUTPUT = """1: 625k_V5_BEDROCK_Geology (Multi Polygon)
2: layer_styles (None)
3: 625k_V5_DYKES_Geology (Multi Polygon)
4: 625k_V5_FAULTS (Multi Line String)
5: 625k_V5_SUPERFICIAL_Geology (Multi Polygon)
6: rtree_bedrock_geom (Multi Polygon) [private]
"""

pytestmark = pytest.mark.skipif(
    shutil.which('sh') is None, reason='needs a POSIX shell')


@pytest.fixture
def stubs(tmp_path):
    """stub ogrinfo/ogr2ogr on PATH; returns the environment to run with"""

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()

    ogrinfo = bin_dir / 'ogrinfo'
    ogrinfo.write_text(
        '#!/bin/sh\n'
        'case "$*" in\n'
        '  *"PG:"*)\n'
        '    for existing in $STUB_EXISTING; do\n'
        '      [ "$3" = "$existing" ] && exit 0\n'
        '    done\n'
        '    exit 1 ;;\n'
        f'  *) cat <<\'LAYERS\'\n{OGRINFO_OUTPUT}LAYERS\n'
        '  ;;\n'
        'esac\n')
    ogrinfo.chmod(0o755)

    ogr2ogr = bin_dir / 'ogr2ogr'
    ogr2ogr.write_text('#!/bin/sh\necho "OGR2OGR $*"\n')
    ogr2ogr.chmod(0o755)

    geopackage = tmp_path / 'geology.gpkg'
    geopackage.write_text('')

    environment = dict(os.environ)
    environment.update({
        'PATH': f"{bin_dir}{os.pathsep}{environment['PATH']}",
        'GPKG_PATH': str(geopackage),
        'GPKG_SRS': 'EPSG:27700',
        'STUB_EXISTING': ''
    })

    return environment


def run(environment, **overrides):
    environment = {**environment, **overrides}
    result = subprocess.run(['sh', str(SCRIPT)], env=environment,
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr

    return result.stdout


def test_layers_and_targets(stubs):
    output = run(stubs)

    assert '625k_V5_BEDROCK_Geology -> public.625k_v5_bedrock_geology' \
        in output
    assert '625k_V5_FAULTS -> public.625k_v5_faults' in output

    # an attribute table and a driver-internal table are not data
    assert 'layer_styles' not in output
    assert 'rtree_bedrock_geom' not in output


def test_ogr2ogr_arguments(stubs):
    output = run(stubs)
    calls = [line for line in output.splitlines()
             if line.startswith('OGR2OGR')]

    assert len(calls) == 4

    for call in calls:
        assert '-f PostgreSQL' in call
        assert '-lco GEOMETRY_NAME=geom' in call
        assert '-lco SPATIAL_INDEX=GIST' in call
        assert '-nlt PROMOTE_TO_MULTI' in call
        assert '-a_srs EPSG:27700' in call


def test_existing_tables_are_skipped(stubs):
    output = run(stubs, STUB_EXISTING='public.625k_v5_bedrock_geology')

    assert 'public.625k_v5_bedrock_geology already loaded, skipping' in output
    assert '625k_v5_bedrock_geology -nlt' not in output
    assert len([line for line in output.splitlines()
                if line.startswith('OGR2OGR')]) == 3


def test_force_reloads_existing_tables(stubs):
    output = run(stubs, STUB_EXISTING='public.625k_v5_bedrock_geology',
                 GPKG_FORCE='true')

    assert 'already loaded' not in output
    assert len([line for line in output.splitlines()
                if line.startswith('OGR2OGR')]) == 4


def test_missing_geopackage_is_not_an_error(stubs):
    output = run(stubs, GPKG_PATH='/does/not/exist.gpkg')

    assert 'nothing to load' in output
    assert 'OGR2OGR' not in output


def test_explicit_layer_selection(stubs):
    output = run(stubs, GPKG_LAYERS='625k_V5_FAULTS')

    assert len([line for line in output.splitlines()
                if line.startswith('OGR2OGR')]) == 1
    assert 'public.625k_v5_faults' in output


def test_crlf_script_is_not_silently_accepted(tmp_path, stubs):
    # the compose entrypoint strips CR before running; prove a CRLF copy
    # really does fail, so that guard is not cargo cult
    crlf = tmp_path / 'load-crlf.sh'
    crlf.write_bytes(SCRIPT.read_bytes().replace(b'\n', b'\r\n'))

    result = subprocess.run(['sh', str(crlf)], env=stubs,
                            capture_output=True, text=True, timeout=60)

    assert result.returncode != 0
