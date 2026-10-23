import json
from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from shapely.geometry import mapping, shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from bosquevivo.api.deps import db_session, get_satellite_provider
from bosquevivo.api.schemas import DeclarationOut, LossOut, ProjectOut, SeriesOut, SeriesPointOut
from bosquevivo.audit.compare import compare_declared_observed
from bosquevivo.audit.service import AuditService
from bosquevivo.core.config import get_settings
from bosquevivo.db.models import ForestLoss, NdviObservation, Project, ProjectDeclaration
from bosquevivo.registry.geometry import simplify_geometry
from bosquevivo.registry.repository import ProjectRepository
from bosquevivo.satellite.models import Sensor, ThumbnailKind
from bosquevivo.satellite.provider import SatelliteProvider
from bosquevivo.satellite.service import SatelliteService

router = APIRouter(prefix="/api/v1/projects", tags=["proyectos"])

# Aligerar un polígono de miles de partes cuesta y la web lo pide en cada visita: se guarda en
# memoria. La clave incluye el hash del polígono, así que un polígono actualizado no queda viejo.
_LIGHT_CACHE: dict[tuple[int, int, float, int | None], dict] = {}
_LIGHT_CACHE_MAX = 2048


def light_geometry(project_id: int, geometry: str, simplify: float, max_parts: int | None) -> dict:
    key = (project_id, hash(geometry), simplify, max_parts)
    cached = _LIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    light = mapping(simplify_geometry(shape(json.loads(geometry)), simplify, max_parts))
    if len(_LIGHT_CACHE) >= _LIGHT_CACHE_MAX:
        _LIGHT_CACHE.clear()
    _LIGHT_CACHE[key] = light
    return light


@router.get("", response_model=list[ProjectOut])
def list_projects(
    country: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(db_session),
):
    return ProjectRepository(session).list_projects(country, limit, offset)


@router.get("/locate", response_model=ProjectOut)
def locate(lat: float, lon: float, session: Session = Depends(db_session)):
    project = ProjectRepository(session).locate(lat, lon)
    if project is None:
        raise HTTPException(status_code=404, detail="ningún proyecto contiene ese punto")
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, session: Session = Depends(db_session)):
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="proyecto no encontrado")
    return project


@router.get("/{project_id}/declarations", response_model=list[DeclarationOut])
def get_declarations(project_id: int, session: Session = Depends(db_session)):
    """Datos declarados o del registro del proyecto (ya revisados): metodología, desarrollador,
    periodo de acreditación, etc."""
    if ProjectRepository(session).get(project_id) is None:
        raise HTTPException(status_code=404, detail="proyecto no encontrado")
    return session.scalars(
        select(ProjectDeclaration)
        .where(ProjectDeclaration.project_id == project_id, ProjectDeclaration.reviewed.is_(True))
        .order_by(ProjectDeclaration.id)
    ).all()


@router.get("/{project_id}/geometry")
def get_geometry(
    project_id: int,
    simplify: float | None = Query(
        default=None,
        ge=0,
        le=0.1,
        description="Tolerancia en grados para aligerar el polígono (p. ej. 0.002 ≈ 220 m).",
    ),
    max_parts: int | None = Query(
        default=None,
        ge=1,
        le=100_000,
        description="Conserva solo las N partes más grandes (para proyectos de miles de parcelas).",
    ),
    session: Session = Depends(db_session),
):
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="proyecto no encontrado")
    if simplify or max_parts:
        geometry = light_geometry(project.id, project.geometry, simplify or 0.0, max_parts)
    else:
        geometry = json.loads(project.geometry)
    return {
        "type": "Feature",
        "properties": {"id": project.id, "name": project.name},
        "geometry": geometry,
    }


@router.get("/{project_id}/thumbnail")
def get_monthly_thumbnail(
    project_id: int,
    month: date | None = None,
    kind: ThumbnailKind = Query(default=ThumbnailKind.NATURAL),
    year: int | None = Query(default=None, ge=2001, le=2024),
    session: Session = Depends(db_session),
    provider: SatelliteProvider | None = Depends(get_satellite_provider),
):
    """Devuelve una capa mensual Sentinel-2 o la pérdida Hansen de un año."""
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="proyecto no encontrado")
    if kind == ThumbnailKind.LOSS:
        if year is None or month is not None:
            raise HTTPException(status_code=422, detail="kind=loss requiere year y no acepta month")
        exists = session.scalar(
            select(ForestLoss.id).where(
                ForestLoss.project_id == project_id,
                ForestLoss.year == year,
            )
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="no hay pérdida Hansen para ese año")
        start = date(year, 1, 1)
        end = date(year + 1, 1, 1)
    else:
        if month is None or year is not None:
            raise HTTPException(status_code=422, detail="month es obligatorio para esta visualización")
        if month.day != 1:
            raise HTTPException(status_code=422, detail="month debe ser el primer día del mes")
        observation = session.scalar(
            select(NdviObservation.id).where(
                NdviObservation.project_id == project_id,
                NdviObservation.period == month,
                NdviObservation.sensor == Sensor.SENTINEL2.value,
            )
        )
        if observation is None:
            raise HTTPException(
                status_code=404, detail="no hay observación Sentinel-2 para ese mes"
            )
        start = month
        end = date(month.year + (month.month == 12), month.month % 12 + 1, 1)

    if provider is None:
        raise HTTPException(status_code=503, detail="el proveedor satelital no está disponible")

    asset = SatelliteService(session, provider, get_settings().images_dir).thumbnail(
        project_id,
        start,
        end,
        kind=kind,
        sensor=Sensor.SENTINEL2,
    )
    min_x, min_y, max_x, max_y = shape(json.loads(project.geometry)).bounds
    return FileResponse(
        asset.path,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Image-Bounds": f"{min_x},{min_y},{max_x},{max_y}",
        },
    )


@router.get("/{project_id}/series", response_model=SeriesOut)
def get_series(project_id: int, session: Session = Depends(db_session)):
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="proyecto no encontrado")
    observations = session.scalars(
        select(NdviObservation)
        .where(NdviObservation.project_id == project_id)
        .order_by(NdviObservation.period)
    ).all()
    losses = session.scalars(
        select(ForestLoss).where(ForestLoss.project_id == project_id).order_by(ForestLoss.year)
    ).all()
    inputs = AuditService(session).build_inputs(project)
    return SeriesOut(
        t0=inputs.t0.isoformat() if inputs.t0 else None,
        ndvi=[
            SeriesPointOut(
                period=o.period.isoformat(),
                sensor=o.sensor,
                ndvi_mean=o.ndvi_mean,
                valid_fraction=o.valid_fraction,
                scene_count=o.scene_count,
            )
            for o in observations
        ],
        loss=[LossOut(year=r.year, ha_lost=r.ha_lost) for r in losses],
        comparison=[asdict(row) for row in compare_declared_observed(inputs)],
    )
