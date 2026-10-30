from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


class Sensor(StrEnum):
    SENTINEL2 = "s2"
    LANDSAT = "landsat"
    MODIS = "modis"


class ThumbnailKind(StrEnum):
    NATURAL = "natural"
    FALSE_COLOR = "false_color"
    NDVI = "ndvi"
    LOSS = "loss"


class TimeSeriesPoint(BaseModel):
    period: date
    sensor: Sensor
    ndvi_mean: float | None = None
    ndvi_p10: float | None = None
    ndvi_p90: float | None = None
    valid_fraction: float = Field(ge=0.0, le=1.0, default=0.0)
    scene_count: int = Field(ge=0, default=0)


class ForestLossPoint(BaseModel):
    year: int
    ha_lost: float = Field(ge=0.0)
    source: str = "hansen"


class ForestBaseline(BaseModel):
    tree_cover_ha: float = Field(ge=0.0)
    threshold_percent: int = 30
    source: str = "hansen"
