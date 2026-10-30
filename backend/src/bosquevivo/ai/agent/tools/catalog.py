import unicodedata

from pydantic import BaseModel, Field

from bosquevivo.ai.agent.tools.base import Tool, ToolContext, ToolOutput
from bosquevivo.ai.agent.tools.data import ProyectoArgs, _project, _summary
from bosquevivo.audit.service import AuditService
from bosquevivo.registry.repository import ProjectRepository

MAX_LISTED = 10

DECLARATION_LABELS = {
    "crediting_start": "inicio_acreditacion",
    "crediting_end": "fin_acreditacion",
    "methodology": "metodologia",
    "developer": "desarrollador",
    "geometry_source": "origen_del_poligono",
    "processing_approach": "forma_de_obtener_el_contorno",
}

SOURCE_NOTICE = (
    "Datos del registro público del proyecto: no incluyen créditos emitidos, toneladas de "
    "carbono ni línea base declarada."
)


class ListarProyectosArgs(BaseModel):
    tipo: str | None = Field(
        default=None,
        description="Deforestación evitada (AD), reforestación (ARR) o manejo forestal (IFM)",
    )
    registro: str | None = Field(default=None, description="Verra, EcoRegistry, Gold Standard...")
    pais: str | None = Field(default=None, description="País del proyecto")
    texto: str | None = Field(default=None, description="Parte del nombre del proyecto")


def _plain(text: str | None) -> str:
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _matches(value: str | None, wanted: str | None) -> bool:
    return not wanted or _plain(wanted) in _plain(value)


def listar_proyectos(ctx: ToolContext, args: ListarProyectosArgs) -> ToolOutput:
    audits = AuditService(ctx.session)
    selected = [
        p
        for p in ProjectRepository(ctx.session).all()
        if _matches(p.project_type, args.tipo)
        and _matches(p.source_registry, args.registro)
        and _matches(p.country, args.pais)
        and _matches(p.name, args.texto)
    ]
    by_type: dict[str, int] = {}
    for project in selected:
        key = project.project_type or "sin tipo"
        by_type[key] = by_type.get(key, 0) + 1
    listed = []
    for project in sorted(selected, key=lambda p: p.name)[:MAX_LISTED]:
        run = audits.latest(project.id)
        listed.append(
            _summary(project)
            | {
                "tipo": project.project_type,
                "categoria": run.result.category if run and run.result else "sin auditoría",
            }
        )
    payload = {
        "total": len(selected),
        "por_tipo": by_type,
        "proyectos": listed,
        "mostrados": len(listed),
    }
    item = ctx.evidence.add("catalogo", "projects", payload)
    return ToolOutput(payload | {"evidencia_id": item.id}, evidence_ids=[item.id])


def obtener_ficha_proyecto(ctx: ToolContext, args: ProyectoArgs) -> ToolOutput:
    project = _project(ctx, args.proyecto_id)
    run = AuditService(ctx.session).latest(project.id)
    declared = {
        DECLARATION_LABELS.get(d.field, d.field): d.value
        for d in project.declarations
        if d.reviewed
    }
    payload = _summary(project) | {
        "tipo": project.project_type,
        "anio_inicio": project.start_year,
        "declarado": declared,
        "tiene_auditoria": bool(run and run.result),
        "fuente": project.source,
        "aviso": SOURCE_NOTICE,
    }
    item = ctx.evidence.add("ficha", f"project:{project.id}", payload)
    return ToolOutput(payload | {"evidencia_id": item.id}, evidence_ids=[item.id])


CATALOG_TOOLS = [
    Tool(
        "listar_proyectos",
        "Lista y cuenta los proyectos registrados, con filtros opcionales por tipo, registro, "
        "país o parte del nombre. Úsala cuando no se sabe el nombre exacto o se pregunta "
        "cuántos o cuáles proyectos hay.",
        ListarProyectosArgs,
        listar_proyectos,
    ),
    Tool(
        "obtener_ficha_proyecto",
        "Ficha del proyecto: tipo, registro, área, desarrollador, metodología y fechas del "
        "periodo de acreditación.",
        ProyectoArgs,
        obtener_ficha_proyecto,
    ),
]
