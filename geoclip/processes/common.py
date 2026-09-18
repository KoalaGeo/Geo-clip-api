# =================================================================
#
# Geo clip API: shared processor helpers
#
# =================================================================

"""Helpers shared by the clip and list-tables processors."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any, Dict, Optional

from pygeoapi.process.base import ProcessorExecuteError

from geoclip.db import ClipDatabase
from geoclip.errors import DatabaseError, InvalidInputError

LOGGER = logging.getLogger(__name__)


class ClipInputError(ProcessorExecuteError):
    """the request cannot be answered as sent"""

    ogc_exception_code = 'InvalidParameterValue'
    http_status_code = HTTPStatus.BAD_REQUEST


class ClipBackendError(ProcessorExecuteError):
    """the database could not answer"""

    ogc_exception_code = 'NoApplicableCode'
    http_status_code = HTTPStatus.INTERNAL_SERVER_ERROR


def database_from_definition(processor_def: Dict[str, Any]) -> ClipDatabase:
    """
    build the database layer from a pygeoapi processor definition

    :param processor_def: processor definition; its optional `data` block
                          holds the connection and publishing settings

    :returns: geoclip.db.ClipDatabase
    """

    config = processor_def.get('data') or {}

    if not isinstance(config, dict):
        raise ClipInputError(
            'processor data block must be a mapping',
            user_msg='invalid plugin configuration')

    return ClipDatabase(config)


def unwrap(value: Any) -> Any:
    """
    unwrap an OGC API - Processes qualified input value

    Clients may send either ``"table": "public.roads"`` or
    ``"table": {"value": "public.roads"}``.

    :param value: raw input value

    :returns: the unwrapped value
    """

    if isinstance(value, dict) and set(value) <= {'value', 'mediaType',
                                                  'encoding', 'schema'}:
        return value.get('value')

    return value


def get_input(data: Dict[str, Any], name: str,
              default: Any = None) -> Optional[Any]:
    """
    read one process input

    :param data: `dict` of process inputs
    :param name: input name
    :param default: value to return when the input is absent or null

    :returns: the input value
    """

    if not isinstance(data, dict):
        raise ClipInputError('process inputs must be an object',
                             user_msg='process inputs must be an object')

    value = unwrap(data.get(name))

    return default if value is None else value


def translate_error(err: Exception) -> ProcessorExecuteError:
    """
    map a geoclip error onto a pygeoapi processor error

    :param err: the raised error

    :returns: `pygeoapi.process.base.ProcessorExecuteError`
    """

    message = str(err)

    if isinstance(err, InvalidInputError):
        return ClipInputError(message, user_msg=message)

    if isinstance(err, DatabaseError):
        LOGGER.error(f'database error: {message}')
        return ClipBackendError(message, user_msg=message)

    LOGGER.exception(err)

    return ClipBackendError(message, user_msg='unexpected error (check logs)')
