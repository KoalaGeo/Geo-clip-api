# =================================================================
#
# Geo clip API: request and response models
#
# =================================================================

"""Request and response models for the download service."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class OutputFormat(str, Enum):
    """the formats a download can be asked for by name

    An enum rather than a plain string so that it renders as a drop-down
    in the API console; the request body still accepts the aliases
    (geopackage, flatgeobuf) that `geoclip.formats` knows about.
    """

    geojson = 'geojson'
    gpkg = 'gpkg'
    fgb = 'fgb'


class OnLimit(str, Enum):
    """what to do with an order larger than the limit"""

    error = 'error'
    truncate = 'truncate'


CLIP_AREA_DESCRIPTION = (
    'Give exactly one of wkt, geometry or bbox.'
)


class ClipArea(BaseModel):
    """the area to clip to, and the table to clip"""

    model_config = ConfigDict(extra='forbid')

    table: str = Field(
        description='Table to clip, as "table" or "schema.table".',
        examples=['public.625k_v5_bedrock_geology'])
    wkt: Optional[str] = Field(
        default=None,
        description=f'POLYGON or MULTIPOLYGON as WKT or EWKT. '
                    f'{CLIP_AREA_DESCRIPTION}')
    geometry: Optional[Union[Dict[str, Any], str]] = Field(
        default=None,
        description=f'GeoJSON geometry, Feature or FeatureCollection; '
                    f'several features are merged. {CLIP_AREA_DESCRIPTION}')
    bbox: Optional[List[float]] = Field(
        default=None, min_length=4, max_length=4,
        description=f'[minx, miny, maxx, maxy] in the srid CRS. '
                    f'{CLIP_AREA_DESCRIPTION}',
        examples=[[-3.30, 55.90, -3.05, 56.02]])
    srid: int = Field(
        default=4326, gt=0,
        description='EPSG code of the clip area.')
    geometry_column: Optional[str] = Field(
        default=None,
        description='Only needed for tables with several geometry columns.')


class EstimateRequest(ClipArea):
    """what an order would contain, without building it"""

    format: Optional[str] = Field(
        default=None,
        description='Format to size the download in (geojson, gpkg, fgb).')
    exact_area: bool = Field(
        default=False,
        description='Union the clipped geometries instead of summing their '
                    'areas. Slower, and only differs when source features '
                    'overlap each other.')
    limit: Optional[int] = Field(
        default=None, gt=0,
        description='Limit to check the row count against; defaults to the '
                    'server limit.')


class ClipRequest(ClipArea):
    """a download"""

    output_srid: Optional[int] = Field(
        default=None, gt=0,
        description='EPSG code of the returned geometries (default 4326).')
    properties: Optional[List[str]] = Field(
        default=None,
        description='Columns to return; all non-geometry columns by '
                    'default.')
    limit: Optional[int] = Field(
        default=None, gt=0,
        description='Maximum features, capped by the server maximum.')
    clip: bool = Field(
        default=True,
        description='False returns intersecting features whole instead of '
                    'cutting them at the boundary.')
    simplify: Optional[Union[bool, float]] = Field(
        default=None,
        description='true simplifies to about a pixel of the clip extent; a '
                    'number sets the tolerance in output CRS units.')
    format: Optional[str] = Field(
        default=None,
        description='geojson (default), gpkg or fgb. The Accept header is '
                    'used when this is not set.')
    on_limit: Literal['error', 'truncate'] = Field(
        default='error',
        description='error refuses an order larger than the limit; truncate '
                    'returns the first N features and says so.')


class TableInfo(BaseModel):
    """a table that can be clipped"""

    name: str = Field(description='Qualified name, as clip accepts it.')
    schema_name: str = Field(alias='schema')
    table: str
    geometry_column: str
    geometry_type: Optional[str] = None
    srid: Optional[int] = None
    coordinate_dimension: Optional[int] = None
    description: Optional[str] = None
    estimated_rows: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


class TableList(BaseModel):
    """the published tables"""

    tables: List[TableInfo]
    count: int
    schemas: List[str]


class EstimateResponse(BaseModel):
    """the size of an order"""

    table: str
    rows: int = Field(description='Features the download would contain.')
    vertices: int = Field(description='Estimated vertices after clipping.')
    requested_area_km2: Optional[float] = Field(
        default=None, description='Area of the clip area itself.')
    covered_area_km2: Optional[float] = Field(
        default=None,
        description='Area of it that has data in it. Zero for point and '
                    'line layers, which have no area.')
    format: str
    estimated_bytes: int = Field(
        description='Estimated download size, within roughly 20%.')
    limit: int = Field(description='Row limit this order was checked '
                                   'against.')
    within_limit: bool


class Problem(BaseModel):
    """RFC 9457 problem details"""

    model_config = ConfigDict(extra='allow')

    type: str = 'about:blank'
    title: str
    status: int
    detail: Optional[str] = None
