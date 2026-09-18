#!/bin/bash
# =================================================================
#
# Geo clip API: container entrypoint
#
# The pygeoapi process manager writes each job's output to a file under
# its `output_dir` and does not create that directory itself, so a fresh
# container fails every execution with
#
#     [Errno 2] No such file or directory: '<output_dir>/<process>-<job id>'
#
# Create the manager's paths here (they may also live on a tmpfs or an
# empty volume at every start), then hand over to the pygeoapi base image
# entrypoint. The paths are read from the running configuration, so a
# mounted config with different paths is honoured too.
#
# =================================================================

set -e

PYTHON=${PYTHON:-/venv/bin/python3}
BASE_ENTRYPOINT=${BASE_ENTRYPOINT:-/entrypoint.sh}

manager_paths() {
    # ask pygeoapi where its process manager writes; print one path per line
    "${PYTHON}" -c "
import os

try:
    from pygeoapi.config import get_config
    manager = get_config().get('server', {}).get('manager') or {}
except Exception:
    manager = {}

connection = manager.get('connection')
paths = [manager.get('output_dir')]

# TinyDB stores jobs in a file; other managers use a URL, which is not ours
# to create
if isinstance(connection, str) and connection.startswith('/'):
    paths.append(os.path.dirname(connection))

for path in paths:
    if path:
        print(path)
" 2>/dev/null || true
}

DEFAULT_OUTPUT_DIR="${GEOCLIP_PROCESS_OUTPUT_DIR:-/tmp/pygeoapi-process-outputs}"
DEFAULT_JOB_DB="${GEOCLIP_JOB_DB:-/tmp/pygeoapi-process-manager.db}"

PATHS=$(manager_paths)
if [ -z "${PATHS}" ]; then
    echo "geoclip: could not read the process manager config; using defaults"
    PATHS=$(printf '%s\n%s\n' "${DEFAULT_OUTPUT_DIR}" \
                              "$(dirname "${DEFAULT_JOB_DB}")")
fi

while IFS= read -r dir; do
    [ -z "${dir}" ] && continue
    if mkdir -p "${dir}" 2>/dev/null; then
        echo "geoclip: process manager directory ${dir} ready"
    else
        echo "geoclip: WARNING could not create ${dir}; process execution" \
             "will fail until it exists and is writable by uid $(id -u)"
    fi
done <<< "${PATHS}"

exec "${BASE_ENTRYPOINT}" "$@"
