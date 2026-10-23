from datetime import date

import pytest

from bosquevivo.satellite.models import Sensor
from bosquevivo.satellite.providers.gee import (
    _advance_date,
    month_starts,
    point_from_properties,
)


def test_advance_date_casts_computed_object_to_ee_date():
    class ComputedObject:
        pass

    class FakeDate:
        def __init__(self, value):
            self.value = value

        def advance(self, delta, unit):
            return ComputedObject()

    class FakeEarthEngine:
        Date = FakeDate

    result = _advance_date(FakeEarthEngine, FakeDate("2024-01-01"), 1, "month")

    assert isinstance(result, FakeDate)
    assert isinstance(result.value, ComputedObject)


def test_month_starts_cubre_cada_mes_hasta_antes_del_final():
    months = month_starts(date(2023, 11, 1), date(2024, 3, 1))

    assert months == [date(2023, 11, 1), date(2023, 12, 1), date(2024, 1, 1), date(2024, 2, 1)]


def test_month_starts_incluye_el_mes_del_inicio_aunque_no_sea_dia_uno():
    assert month_starts(date(2020, 5, 17), date(2020, 7, 1)) == [date(2020, 5, 1), date(2020, 6, 1)]


def test_month_starts_vacio_si_el_rango_no_tiene_meses():
    assert month_starts(date(2020, 5, 1), date(2020, 5, 1)) == []


def test_month_starts_cruza_varios_anos():
    months = month_starts(date(2010, 1, 1), date(2025, 10, 1))

    assert len(months) == 15 * 12 + 9
    assert months[0] == date(2010, 1, 1) and months[-1] == date(2025, 9, 1)


def test_un_mes_con_escenas_se_convierte_en_punto():
    point = point_from_properties(
        {
            "period": "2019-03-01",
            "count": 6,
            "mean": 0.812,
            "p10": 0.7,
            "p90": 0.9,
            "valid": 800,
            "total": 1000,
        },
        Sensor.SENTINEL2,
    )

    assert point is not None
    assert point.period == date(2019, 3, 1)
    assert point.sensor == Sensor.SENTINEL2
    assert point.ndvi_mean == pytest.approx(0.812)
    assert point.valid_fraction == pytest.approx(0.8)
    assert point.scene_count == 6


def test_un_mes_sin_escenas_se_descarta():
    assert point_from_properties({"period": "2019-03-01", "count": 0}, Sensor.LANDSAT) is None
    assert point_from_properties({"period": "2019-03-01"}, Sensor.LANDSAT) is None


def test_la_fraccion_valida_nunca_pasa_de_uno_ni_divide_por_cero():
    over = point_from_properties(
        {"period": "2019-03-01", "count": 2, "valid": 120, "total": 100}, Sensor.LANDSAT
    )
    nothing = point_from_properties(
        {"period": "2019-03-01", "count": 2, "mean": None, "valid": None, "total": 0},
        Sensor.LANDSAT,
    )

    assert over is not None and over.valid_fraction == 1.0
    assert nothing is not None and nothing.valid_fraction == 0.0 and nothing.ndvi_mean is None


def test_mes_sin_pixeles_ndvi_validos_no_convierte_valores_por_defecto_en_cero():
    point = point_from_properties(
        {
            "period": "2019-03-01",
            "count": 2,
            "mean": 0,
            "p10": 0,
            "p90": 0,
            "valid": 0,
            "total": 100,
        },
        Sensor.SENTINEL2,
    )

    assert point is not None
    assert point.ndvi_mean is None
    assert point.ndvi_p10 is None
    assert point.ndvi_p90 is None
    assert point.valid_fraction == 0.0


def test_acepta_fechas_con_hora():
    point = point_from_properties({"period": "2019-03-01T00:00:00", "count": 1}, Sensor.LANDSAT)

    assert point is not None and point.period == date(2019, 3, 1)
