# =================================================================
#
# Geo clip API: standalone HTTP service
#
# =================================================================

"""A FastAPI service over the same clipping library the pygeoapi plugins
use, shaped for downloads rather than for OGC API - Processes."""

from geoclip.api.app import app, create_app

__all__ = ['app', 'create_app']
