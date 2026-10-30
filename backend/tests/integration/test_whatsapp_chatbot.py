import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bosquevivo.ai.agent import Agent, default_registry
from bosquevivo.ai.llm import LLMResponse, ScriptedLLM, ToolCall
from bosquevivo.api.app import create_app
from bosquevivo.api.deps import db_session
from bosquevivo.assistant import AssistantService, IncomingMessage, texts
from bosquevivo.assistant.client import RecordingClient
from bosquevivo.assistant.media import strip_metadata
from bosquevivo.assistant.runtime import build_llm
from bosquevivo.assistant.signature import compute_signature
from bosquevivo.core.config import Settings, get_settings
from bosquevivo.core.errors import ExternalServiceError
from bosquevivo.core.privacy import phone_hmac
from bosquevivo.db.models import Job, KbChunk
from bosquevivo.db.session import _default_factory, get_engine, get_session
from bosquevivo.jobs.queue import MAX_ATTEMPTS, JobQueue

PHONE = "573001112233"
OTHER_PHONE = "573009998877"
SETTINGS = Settings(phone_hmac_key="clave-hmac-de-prueba", whatsapp_app_secret="app-secret")


def jpeg_with_exif() -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "CamaraDePrueba"  # fabricante
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def build(db, script, media=None):
    llm = ScriptedLLM(script)
    client = RecordingClient(media)
    agent = Agent(db, llm, default_registry(), SETTINGS)
    return AssistantService(db, agent, client, llm, SETTINGS), client, llm


def text(message_id, body, phone=PHONE):
    return IncomingMessage(message_id=message_id, phone=phone, kind="text", text=body)


def onboard(service, media=False):
    service.handle(text("m0", "hola"))
    service.handle(text("m1", "ACEPTO"))
    if media:
        service.handle(text("m1b", "ACEPTO FOTOS"))


def whatsapp_job(queue, message_id, phone):
    return queue.enqueue(
        "whatsapp_message",
        {"message_id": message_id, "phone": phone, "kind": "text", "text": "hola"},
    )


def test_strip_metadata_removes_exif():
    clean, mime = strip_metadata(jpeg_with_exif())
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(clean)) as image:
        assert not image.getexif()


def test_strip_metadata_rejects_files_that_are_not_images():
    with pytest.raises(ExternalServiceError):
        strip_metadata(b"esto no es una foto")


def test_photo_reaches_the_model_without_exif_and_gets_an_acknowledgement(db):
    script = [
        LLMResponse(tool_calls=[ToolCall("analizar_foto_usuario", {})]),
        LLMResponse(text="Se ven árboles y un claro pequeño."),
        LLMResponse(text="En la foto se ven árboles y un claro. Es evidencia no verificada."),
    ]
    service, client, llm = build(db, script, media={"img1": (jpeg_with_exif(), "image/jpeg")})
    onboard(service, media=True)
    client.sent.clear()
    service.handle(IncomingMessage(message_id="m2", phone=PHONE, kind="image", media_id="img1"))
    assert client.sent[0][1] == texts.WORKING
    assert "no verificada" in client.sent[-1][1]
    sent_photo = llm.calls[0]["messages"][-1].media[0]
    assert sent_photo.mime_type == "image/jpeg"
    with Image.open(io.BytesIO(sent_photo.data)) as image:
        assert not image.getexif()


def test_invalid_photo_gets_a_friendly_failure(db):
    service, client, llm = build(db, [], media={"img1": (b"basura", "image/jpeg")})
    onboard(service, media=True)
    client.sent.clear()
    service.handle(IncomingMessage(message_id="m2", phone=PHONE, kind="image", media_id="img1"))
    assert client.sent == [(PHONE, texts.FAILURE)]
    assert llm.calls == []


def test_quick_question_has_no_acknowledgement(db):
    service, client, _ = build(db, [LLMResponse(text="Hola, ¿sobre qué proyecto quiere saber?")])
    onboard(service)
    client.sent.clear()
    service.handle(text("m2", "hola"))
    assert len(client.sent) == 1 and client.sent[0][1] != texts.WORKING


def test_location_without_longitude_is_not_used(db):
    service, _, llm = build(db, [LLMResponse(text="No pude leer bien la ubicación.")])
    onboard(service)
    service.handle(IncomingMessage(message_id="m2", phone=PHONE, kind="location", latitude=5.0))
    assert "Ubicación enviada" not in (llm.calls[0]["messages"][-1].text or "")


def test_completed_whatsapp_job_forgets_phone_and_text(db):
    queue = JobQueue(db)
    whatsapp_job(queue, "w1", PHONE)
    job = queue.claim_next()
    queue.complete(job, {"message_id": "w1"})
    assert job.payload == {"message_id": "w1", "kind": "text"}


def test_failed_whatsapp_job_forgets_phone_only_after_last_attempt(db):
    queue = JobQueue(db)
    whatsapp_job(queue, "w1", PHONE)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        job = queue.claim_next()
        queue.fail(job, "error")
        assert ("phone" in job.payload) is (attempt < MAX_ATTEMPTS)
    assert job.status == "failed"


def test_delete_command_also_removes_queued_messages_with_the_phone(db):
    queue = JobQueue(db)
    whatsapp_job(queue, "w1", PHONE)
    whatsapp_job(queue, "w2", OTHER_PHONE)
    service, client, _ = build(db, [])
    onboard(service)
    service.handle(text("m9", "borrar mis datos"))
    remaining = [j.payload["phone"] for j in db.query(Job).all()]
    assert remaining == [OTHER_PHONE]
    assert client.sent[-1][1] == texts.DELETED


def test_phone_hmac_requires_a_key():
    with pytest.raises(ValueError):
        phone_hmac(PHONE, "")


def webhook_body() -> bytes:
    message = {"id": "w1", "from": PHONE, "type": "text", "text": {"body": "hola"}}
    return json.dumps({"entry": [{"changes": [{"value": {"messages": [message]}}]}]}).encode()


@pytest.mark.parametrize(
    "settings",
    [
        Settings(whatsapp_app_secret="app-secret", phone_hmac_key=""),
        Settings(whatsapp_app_secret="app-secret", app_env="prod"),
    ],
)
def test_webhook_refuses_messages_without_a_real_hmac_key(client, db, settings):
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.dependency_overrides[db_session] = lambda: db
    body = webhook_body()
    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": compute_signature(body, "app-secret")},
    )
    assert response.status_code == 503
    assert db.query(Job).count() == 0


def test_chat_and_whatsapp_share_one_rate_limiter():
    settings = Settings(gemini_api_key="clave-falsa", gemini_model="modelo")
    first, second = build_llm(settings), build_llm(settings)
    assert first.limiter is second.limiter  # type: ignore[attr-defined]


def test_startup_loads_the_knowledge_base(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'kb.db'}")
    monkeypatch.setenv("SATELLITE_PROVIDER", "fake")
    get_settings.cache_clear()
    get_engine.cache_clear()
    _default_factory.cache_clear()
    try:
        with TestClient(create_app()), get_session() as session:
            assert session.query(KbChunk).count() > 10
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        _default_factory.cache_clear()
