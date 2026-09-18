# =================================================================
#
# Geo clip API: error types
#
# =================================================================

"""Errors raised by the database layer.

These are deliberately independent of pygeoapi so that :mod:`geoclip.db`
can be used (and tested) without a pygeoapi installation.  The processors
translate them into `pygeoapi.process.base.ProcessorExecuteError`.
"""


class GeoClipError(Exception):
    """base class for all geoclip errors"""


class InvalidInputError(GeoClipError):
    """user supplied something we will not send to the database"""


class TableNotFoundError(InvalidInputError):
    """the requested table is unknown, not spatial, or not allowed"""


class DatabaseError(GeoClipError):
    """the database refused or failed to answer"""
