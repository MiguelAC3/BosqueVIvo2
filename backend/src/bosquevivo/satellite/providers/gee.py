import json
from datetime import date
from pathlib import Path

import ee
import httpx

from bosquevivo.core.errors import ExternalServiceError
from bosquevivo.core.logging import get_logger
from bosquevivo.satellite.models import (
    ForestBaseline,
    ForestLossPoint,
    Sensor,
    ThumbnailKind,
    TimeSeriesPoint,
)

log = get_logger(__name__)

SENTINEL2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
CLOUD_SCORE_COLLECTION = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"
LANDSAT_COLLECTIONS = (
    "LANDSAT/LT05/C02/T1_L2",
    "LANDSAT/LE07/C02/T1_L2",
    "LANDSAT/LC08/C02/T1_L2",
    "LANDSAT/LC09/C02/T1_L2",
)
HANSEN_DATASET = "UMD/hansen/global_forest_change_2024_v1_12"
CLOUD_SCORE_THRESHOLD = 0.6
TREE_COVER_THRESHOLD = 30
MAX_PIXELS = 1_000_000_000


BATCH_MONTHS = 12  # meses por llamada; mantiene cada petición bajo el límite de 5 min de getInfo


def month_starts(start: date, end: date) -> list[date]:
    """Primer día de cada mes desde `start` hasta antes de `end`."""
    months: list[date] = []
    year, month = start.year, start.month
    while date(year, month, 1) < end:
        months.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def point_from_properties(props: dict, sensor: Sensor) -> TimeSeriesPoint | None:
    """Convierte las propiedades de un mes (calculadas en el servidor de Earth Engine) en un punto.
    Los meses sin escenas se descartan."""
    count = int(props.get("count") or 0)
    if not count:
        return None
    valid = props.get("valid") or 0
    total = props.get("total") or 0
    return TimeSeriesPoint(
        period=date.fromisoformat(str(props["period"])[:10]),
        sensor=sensor,
        ndvi_mean=props.get("mean") if valid else None,
        ndvi_p10=props.get("p10") if valid else None,
        ndvi_p90=props.get("p90") if valid else None,
        valid_fraction=min(1.0, valid / total) if total else 0.0,
        scene_count=count,
    )


def _scale_for(area_ha: float) -> int:
    if area_ha < 2_000:
        return 10
    if area_ha < 20_000:
        return 30
    return 60


def _info(value) -> dict:
    return value.getInfo() or {}


def _advance_date(ee, value, delta, unit):
    return ee.Date(ee.Date(value).advance(delta, unit))


class GeeProvider:
    name = "gee"

    def __init__(self, project: str, credentials_path: str = "") -> None:
        if not project:
            raise ExternalServiceError("GEE_PROJECT no está configurado")
        self._ee = ee
        try:
            if credentials_path:
                key_file = Path(credentials_path)
                if not key_file.is_file():
                    raise ExternalServiceError(
                        f"no se encontró el archivo de credenciales de Earth Engine: {key_file}"
                    )
                key_data = json.loads(key_file.read_text(encoding="utf-8"))
                service_account = key_data.get("client_email")
                if not service_account:
                    raise ExternalServiceError(
                        "el JSON de credenciales no contiene client_email"
                    )
                credentials = ee.ServiceAccountCredentials(
                    service_account, key_file=str(key_file)
                )
                ee.Initialize(credentials, project=project)
            else:
                ee.Initialize(project=project)
        except Exception as exc:
            if isinstance(exc, ExternalServiceError):
                raise
            raise ExternalServiceError(f"no se pudo iniciar Earth Engine: {exc}") from exc

    def _region(self, geometry: dict):
        return self._ee.Geometry(json.loads(json.dumps(geometry)))

    def _area_ha(self, region) -> float:
        return float(region.area(maxError=10).getInfo()) / 10_000.0

    def _s2_collection(self, region, start: date, end: date):
        ee = self._ee
        scores = ee.ImageCollection(CLOUD_SCORE_COLLECTION)
        collection = (
            ee.ImageCollection(SENTINEL2_COLLECTION)
            .filterBounds(region)
            .filterDate(start.isoformat(), end.isoformat())
            .linkCollection(scores, ["cs_cdf"])
        )

        def prepare(image):
            valid = image.select("cs_cdf").gte(CLOUD_SCORE_THRESHOLD)
            index = image.normalizedDifference(["B8", "B4"]).rename("ndvi")
            return index.addBands(valid.rename("valid")).copyProperties(
                image, ["system:time_start"]
            )

        return collection.map(prepare)

    def _landsat_collection(self, region, start: date, end: date):
        ee = self._ee

        def prepare(image, nir: str, red: str):
            qa = image.select("QA_PIXEL")
            clear = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
            scaled = image.select([nir, red]).multiply(0.0000275).add(-0.2)
            index = scaled.normalizedDifference([nir, red]).rename("ndvi")
            return index.addBands(clear.rename("valid")).copyProperties(
                image, ["system:time_start"]
            )

        merged = ee.ImageCollection([])
        for name in LANDSAT_COLLECTIONS:
            bands = ("SR_B5", "SR_B4") if "LC0" in name else ("SR_B4", "SR_B3")
            part = (
                ee.ImageCollection(name)
                .filterBounds(region)
                .filterDate(start.isoformat(), end.isoformat())
                .map(lambda img, b=bands: prepare(img, b[0], b[1]))
            )
            merged = merged.merge(part)
        return merged

    def _timeseries_batched(
        self, collection, region, *, scale: int, start: date, end: date, sensor: Sensor
    ) -> list[TimeSeriesPoint]:
        """Calcula las estadísticas mensuales dentro de Earth Engine y trae 12 meses por llamada."""
        ee = self._ee
        reducer = (
            ee.Reducer.mean()
            .combine(ee.Reducer.percentile([10, 90]), sharedInputs=True)
            .combine(ee.Reducer.count(), sharedInputs=True)
        )
        months = month_starts(start, end)
        points: list[TimeSeriesPoint] = []
        for offset in range(0, len(months), BATCH_MONTHS):
            chunk = months[offset : offset + BATCH_MONTHS]
            origin = ee.Date(chunk[0].isoformat())

            def per_month(index):
                first_day = _advance_date(ee, origin, ee.Number(index), "month")
                next_day = _advance_date(ee, first_day, 1, "month")
                monthly = collection.filterDate(first_day, next_day)
                count = monthly.size()
                composite = monthly.map(
                    lambda img: img.select("ndvi").updateMask(img.select("valid"))
                ).median()
                stats = composite.reduceRegion(
                    reducer, region, scale, maxPixels=MAX_PIXELS, bestEffort=True
                )
                first_scene_pixels = ee.Number(
                    ee.Algorithms.If(
                        count.gt(0),
                        ee.Dictionary(
                            monthly.first()
                            .select("ndvi")
                            .reduceRegion(
                                ee.Reducer.count(),
                                region,
                                scale,
                                maxPixels=MAX_PIXELS,
                                bestEffort=True,
                            )
                        ).get("ndvi", 0),
                        0,
                    )
                )
                return ee.Feature(
                    None,
                    {
                        "period": first_day.format("YYYY-MM-dd"),
                        "count": count,
                        "mean": stats.get("ndvi_mean", 0),
                        "p10": stats.get("ndvi_p10", 0),
                        "p90": stats.get("ndvi_p90", 0),
                        "valid": stats.get("ndvi_count", 0),
                        "total": first_scene_pixels,
                    },
                )

            rows = ee.FeatureCollection(ee.List.sequence(0, len(chunk) - 1).map(per_month))
            for feature in _info(rows).get("features", []):
                point = point_from_properties(feature.get("properties", {}), sensor)
                if point is not None:
                    points.append(point)
        return points

    def get_timeseries(
        self, geometry: dict, start: date, end: date, sensor: Sensor
    ) -> list[TimeSeriesPoint]:
        region = self._region(geometry)
        scale = _scale_for(self._area_ha(region))
        collection = (
            self._s2_collection(region, start, end)
            if sensor == Sensor.SENTINEL2
            else self._landsat_collection(region, start, end)
        )
        try:
            return self._timeseries_batched(
                collection, region, scale=scale, start=start, end=end, sensor=sensor
            )
        except Exception as exc:  # cualquier fallo de Earth Engine
            log.warning("gee_batch_failed_using_sequential", error=str(exc), sensor=sensor.value)
            return self._timeseries_sequential(
                collection, region, scale=scale, start=start, end=end, sensor=sensor
            )

    def _timeseries_sequential(
        self, collection, region, *, scale: int, start: date, end: date, sensor: Sensor
    ) -> list[TimeSeriesPoint]:
        """Versión original: tres llamadas bloqueantes por mes. Lenta, pero es el respaldo."""
        ee = self._ee
        reducer = (
            ee.Reducer.mean()
            .combine(ee.Reducer.percentile([10, 90]), sharedInputs=True)
            .combine(ee.Reducer.count(), sharedInputs=True)
        )
        points: list[TimeSeriesPoint] = []
        year, month = start.year, start.month
        while date(year, month, 1) < end:
            nxt = date(year + (month == 12), month % 12 + 1, 1)
            monthly = collection.filterDate(date(year, month, 1).isoformat(), nxt.isoformat())
            count = int(monthly.size().getInfo())
            if count:
                masked = monthly.map(lambda img: img.select("ndvi").updateMask(img.select("valid")))
                composite = masked.median()
                stats = _info(
                    composite.reduceRegion(
                        reducer, region, scale, maxPixels=MAX_PIXELS, bestEffort=True
                    )
                )
                total = (
                    monthly.first()
                    .select("ndvi")
                    .reduceRegion(
                        ee.Reducer.count(), region, scale, maxPixels=MAX_PIXELS, bestEffort=True
                    )
                    .getInfo()
                    .get("ndvi")
                    or 0
                )
                valid = stats.get("ndvi_count") or 0
                points.append(
                    TimeSeriesPoint(
                        period=date(year, month, 1),
                        sensor=sensor,
                        ndvi_mean=stats.get("ndvi_mean"),
                        ndvi_p10=stats.get("ndvi_p10"),
                        ndvi_p90=stats.get("ndvi_p90"),
                        valid_fraction=min(1.0, valid / total) if total else 0.0,
                        scene_count=count,
                    )
                )
            year, month = nxt.year, nxt.month
        return points

    def get_forest_loss(self, geometry: dict) -> list[ForestLossPoint]:
        ee = self._ee
        region = self._region(geometry)
        hansen = ee.Image(HANSEN_DATASET)
        forest = hansen.select("treecover2000").gte(TREE_COVER_THRESHOLD)
        loss_year = hansen.select("lossyear").updateMask(forest)
        area = ee.Image.pixelArea().divide(10_000)
        grouped = _info(
            area.addBands(loss_year).reduceRegion(
                ee.Reducer.sum().group(groupField=1, groupName="year"),
                region,
                30,
                maxPixels=MAX_PIXELS,
                bestEffort=True,
            )
        )
        result: list[ForestLossPoint] = []
        for group in grouped.get("groups", []):
            code = int(group["year"])
            if code > 0:
                result.append(ForestLossPoint(year=2000 + code, ha_lost=float(group["sum"])))
        return sorted(result, key=lambda p: p.year)

    def get_forest_baseline(self, geometry: dict) -> ForestBaseline:
        ee = self._ee
        region = self._region(geometry)
        forest = ee.Image(HANSEN_DATASET).select("treecover2000").gte(TREE_COVER_THRESHOLD)
        total = _info(
            ee.Image.pixelArea()
            .divide(10_000)
            .updateMask(forest)
            .reduceRegion(ee.Reducer.sum(), region, 30, maxPixels=MAX_PIXELS, bestEffort=True)
        ).get("area", 0.0)
        return ForestBaseline(
            tree_cover_ha=float(total or 0.0), threshold_percent=TREE_COVER_THRESHOLD
        )

    def get_thumbnail(
        self, geometry: dict, start: date, end: date, kind: ThumbnailKind, sensor: Sensor
    ) -> bytes:
        ee = self._ee
        region = self._region(geometry)
        if kind == ThumbnailKind.NDVI:
            collection = self._s2_collection(region, start, end).map(
                lambda image: image.select("ndvi").updateMask(image.select("valid"))
            )
            image = collection.median().clip(region).visualize(
                min=-1,
                max=1,
                palette=["#8c510a", "#d8b365", "#f6e8c3", "#a6d96a", "#1a9641", "#00441b"],
            )
            url = image.getThumbURL({"region": region, "dimensions": 512, "format": "png"})
            response = httpx.get(url, timeout=120)
            if response.status_code != 200:
                raise ExternalServiceError(f"error al obtener la miniatura: {response.status_code}")
            return response.content

        if kind == ThumbnailKind.LOSS:
            hansen = self._ee.Image(HANSEN_DATASET)
            forest = hansen.select("treecover2000").gte(TREE_COVER_THRESHOLD)
            loss = (
                hansen.select("lossyear")
                .eq(start.year - 2000)
                .And(forest)
                .selfMask()
                .clip(region)
            )
            image = loss.visualize(min=0, max=1, palette=["#e31a1c"])
            url = image.getThumbURL({"region": region, "dimensions": 512, "format": "png"})
            response = httpx.get(url, timeout=120)
            if response.status_code != 200:
                raise ExternalServiceError(f"error al obtener la miniatura: {response.status_code}")
            return response.content

        s2 = (
            ee.ImageCollection(SENTINEL2_COLLECTION)
            .filterBounds(region)
            .filterDate(start.isoformat(), end.isoformat())
            .linkCollection(ee.ImageCollection(CLOUD_SCORE_COLLECTION), ["cs_cdf"])
            .map(lambda img: img.updateMask(img.select("cs_cdf").gte(CLOUD_SCORE_THRESHOLD)))
            .median()
            .clip(region)
        )
        if kind == ThumbnailKind.NATURAL:
            image = s2.visualize(bands=["B4", "B3", "B2"], min=0, max=3000)
        else:
            image = s2.visualize(bands=["B8", "B4", "B3"], min=0, max=4000)
        url = image.getThumbURL({"region": region, "dimensions": 512, "format": "png"})
        response = httpx.get(url, timeout=120)
        if response.status_code != 200:
            raise ExternalServiceError(f"error al obtener la miniatura: {response.status_code}")
        return response.content

    def dataset_versions(self) -> dict[str, str]:
        return {
            "sentinel2": SENTINEL2_COLLECTION,
            "cloud_score": CLOUD_SCORE_COLLECTION,
            "landsat": ",".join(LANDSAT_COLLECTIONS),
            "hansen": HANSEN_DATASET,
        }
