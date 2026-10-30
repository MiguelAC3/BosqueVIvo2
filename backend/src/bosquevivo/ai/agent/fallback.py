from bosquevivo.ai.agent.evidence import EvidenceItem

MAX_COMMUNITY_CHARS = 300
HUMAN_CONTACT = "Si quiere, una persona del equipo puede ayudarle."
BUSY = (
    "En este momento hay muchas consultas y no pude analizar su pregunta. "
    "Intente de nuevo en un par de minutos."
)


def _audit_item(items: list[EvidenceItem]) -> EvidenceItem | None:
    for item in reversed(items):
        if item.kind == "auditoria" and item.payload.get("disponible"):
            return item
    return None


def fallback_answer(items: list[EvidenceItem], role: str, reason: str = "") -> str:
    audit = _audit_item(items)
    if audit and audit.payload.get("indice") is not None:
        p = audit.payload
        name = str(p["proyecto"])[:60]
        text = (
            f"Según el último cálculo ({p['fecha_calculo']}), {name} tiene una correspondencia "
            f"observada {str(p['categoria']).lower()} ({p['indice']:.0f} de {p['escala_maxima']}) "
            f"con confianza {p['confianza']}. Es lo que se ve desde el satélite, no una acusación."
        )
    elif audit:
        text = (
            "Todavía no hay datos suficientes para concluir sobre ese proyecto. "
            "Puede deberse a nubes o a series cortas."
        )
    elif reason == "cuota":
        text = BUSY
    else:
        text = (
            "No puedo responder con seguridad en este momento porque no tengo datos "
            "suficientes. El análisis profundo puede no estar disponible ahora."
        )
    if role == "comunidad" and len(text) + len(HUMAN_CONTACT) + 1 <= MAX_COMMUNITY_CHARS:
        text = f"{text} {HUMAN_CONTACT}"
    return text
