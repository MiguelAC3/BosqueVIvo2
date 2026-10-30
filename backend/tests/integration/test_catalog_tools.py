import json
from datetime import date

import pytest
from pydantic import BaseModel

from bosquevivo.ai.agent import Agent, AgentRequest, default_registry
from bosquevivo.ai.agent.evidence import EvidenceStore
from bosquevivo.ai.agent.tools import Tool, ToolContext, ToolRegistry
from bosquevivo.ai.agent.tools.catalog import (
    ListarProyectosArgs,
    listar_proyectos,
    obtener_ficha_proyecto,
)
from bosquevivo.ai.agent.tools.data import ProyectoArgs
from bosquevivo.ai.llm import LLMResponse, ScriptedLLM, ToolCall
from bosquevivo.audit import AuditService
from bosquevivo.core.config import Settings
from bosquevivo.core.errors import ExternalServiceError
from bosquevivo.db.models import AgentStep, Project, ProjectDeclaration
from bosquevivo.satellite import SatelliteService, Sensor
from bosquevivo.satellite.providers import FakeProvider

RING = [[-75.0, 5.0], [-74.9, 5.0], [-74.9, 5.1], [-75.0, 5.1], [-75.0, 5.0]]
SETTINGS = Settings(admin_token="admin-secret-token")


def project(name, project_type, registry):
    return Project(
        source_registry=registry,
        registry_id=name[:3],
        name=name,
        country="Colombia",
        project_type=project_type,
        start_year=2013,
        area_ha=1000.0,
        geometry=json.dumps({"type": "Polygon", "coordinates": [RING]}),
        source="test",
    )


@pytest.fixture
def catalog(db, tmp_path):
    acapa = project("Acapa REDD+", "Deforestación evitada (AD)", "Verra")
    cacao = project("Cacao Reforestado", "Reforestación (ARR)", "EcoRegistry")
    db.add_all([acapa, cacao])
    db.flush()
    db.add_all(
        [
            ProjectDeclaration(project_id=acapa.id, field="developer", value="Consejo Acapa"),
            ProjectDeclaration(project_id=acapa.id, field="crediting_start", value="2013-11-13"),
        ]
    )
    db.commit()
    sat = SatelliteService(db, FakeProvider(), tmp_path)
    sat.refresh_timeseries(
        acapa.id, date(2016, 1, 1), date(2024, 12, 31), sensors=(Sensor.SENTINEL2,)
    )
    sat.refresh_forest_loss(acapa.id)
    AuditService(db).run(acapa.id)
    db.commit()
    return acapa, cacao


def context(db):
    return ToolContext(session=db, settings=SETTINGS, evidence=EvidenceStore(db), role="comunidad")


def call(name, **args):
    return LLMResponse(tool_calls=[ToolCall(name, args)])


def test_list_projects_counts_and_filters_without_accents(db, catalog):
    everything = listar_proyectos(context(db), ListarProyectosArgs()).content
    assert everything["total"] == 2
    assert everything["por_tipo"] == {"Deforestación evitada (AD)": 1, "Reforestación (ARR)": 1}
    reforestation = listar_proyectos(context(db), ListarProyectosArgs(tipo="reforestacion"))
    assert [p["nombre"] for p in reforestation.content["proyectos"]] == ["Cacao Reforestado"]
    assert reforestation.content["proyectos"][0]["categoria"] == "sin auditoría"
    assert reforestation.evidence_ids


def test_list_projects_shows_audit_category(db, catalog):
    result = listar_proyectos(context(db), ListarProyectosArgs(registro="verra")).content
    assert result["total"] == 1
    assert result["proyectos"][0]["categoria"] != "sin auditoría"


def test_project_sheet_translates_declarations_and_warns_about_missing_credits(db, catalog):
    acapa, _ = catalog
    sheet = obtener_ficha_proyecto(context(db), ProyectoArgs(proyecto_id=acapa.id)).content
    assert sheet["declarado"] == {
        "desarrollador": "Consejo Acapa",
        "inicio_acreditacion": "2013-11-13",
    }
    assert sheet["tiene_auditoria"] is True
    assert "toneladas" in sheet["aviso"]


def test_catalog_tools_are_available_to_the_community():
    names = default_registry().names("comunidad")
    assert {"listar_proyectos", "obtener_ficha_proyecto"} <= set(names)


def test_follow_up_question_receives_previous_turn(db, catalog):
    acapa, _ = catalog
    script = [
        call("obtener_auditoria", proyecto_id=acapa.id),
        LLMResponse(text="Acapa tiene correspondencia alta, con confianza alta."),
        call("obtener_perdida_anual", proyecto_id=acapa.id),
        LLMResponse(text="La pérdida se mantiene baja, con confianza alta."),
    ]
    llm = ScriptedLLM(script)
    agent = Agent(db, llm, default_registry(), SETTINGS)
    agent.answer(AgentRequest(text="¿Cómo está Acapa?", user_ref="wa:1"))
    agent.answer(AgentRequest(text="¿Y cuánto bosque perdió?", user_ref="wa:1"))
    follow_up = llm.calls[2]["messages"]
    assert "mensaje_previo" in (follow_up[0].text or "")
    assert "Acapa" in (follow_up[0].text or "")
    assert follow_up[1].role == "model" and "correspondencia alta" in (follow_up[1].text or "")


def test_history_is_not_shared_between_people(db, catalog):
    script = [
        LLMResponse(text="Hola, puedo contarle cómo está un proyecto."),
        LLMResponse(text="Hola, ¿sobre qué proyecto quiere saber?"),
    ]
    llm = ScriptedLLM(script)
    agent = Agent(db, llm, default_registry(), SETTINGS)
    agent.answer(AgentRequest(text="hola", user_ref="wa:1"))
    agent.answer(AgentRequest(text="hola", user_ref="wa:2"))
    assert len(llm.calls[1]["messages"]) == 1


def test_history_can_be_disabled(db, catalog):
    llm = ScriptedLLM([LLMResponse(text="Hola."), LLMResponse(text="Hola de nuevo.")])
    agent = Agent(
        db, llm, default_registry(), Settings(admin_token="x" * 10, agent_history_turns=0)
    )
    agent.answer(AgentRequest(text="hola", user_ref="wa:1"))
    agent.answer(AgentRequest(text="hola", user_ref="wa:1"))
    assert len(llm.calls[1]["messages"]) == 1


def test_unexpected_model_error_returns_fallback_instead_of_crashing(db, catalog):
    def broken(*_):
        raise RuntimeError("conexión reiniciada")

    agent = Agent(db, ScriptedLLM(broken), default_registry(), SETTINGS)
    answer = agent.answer(AgentRequest(text="¿Cómo está Acapa?", user_ref="wa:1"))
    assert answer.used_fallback
    assert "no tengo datos" in answer.text


class SinArgs(BaseModel):
    pass


def test_external_failure_inside_a_tool_lets_the_agent_continue(db, catalog):
    def down(ctx, args):
        raise ExternalServiceError("Earth Engine no responde")

    registry = ToolRegistry([Tool("satelite", "Consulta el satélite.", SinArgs, down)])
    script = [call("satelite"), LLMResponse(text="El satélite no está disponible ahora.")]
    agent = Agent(db, ScriptedLLM(script), registry, SETTINGS)
    answer = agent.answer(AgentRequest(text="analiza las imágenes", user_ref="wa:1"))
    assert not answer.used_fallback
    step = db.query(AgentStep).one()
    assert step.error == "Earth Engine no responde"
