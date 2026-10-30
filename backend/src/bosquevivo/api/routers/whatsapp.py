from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from bosquevivo.api.deps import db_session
from bosquevivo.assistant.messages import parse_webhook
from bosquevivo.assistant.signature import verify_signature
from bosquevivo.core.config import Settings, get_settings
from bosquevivo.core.privacy import INSECURE_KEYS
from bosquevivo.jobs.kinds import WHATSAPP_MESSAGE
from bosquevivo.jobs.queue import JobQueue

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])


@router.get("", response_class=PlainTextResponse)
def verify(
    mode: str = Query(alias="hub.mode", default=""),
    token: str = Query(alias="hub.verify_token", default=""),
    challenge: str = Query(alias="hub.challenge", default=""),
    settings: Settings = Depends(get_settings),
):
    expected = settings.whatsapp_verify_token
    if mode == "subscribe" and expected and token == expected:
        return challenge
    raise HTTPException(status_code=403, detail="verificación inválida")


@router.post("")
async def receive(
    request: Request,
    session: Session = Depends(db_session),
    settings: Settings = Depends(get_settings),
):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(body, signature, settings.whatsapp_app_secret):
        raise HTTPException(status_code=401, detail="firma inválida")
    insecure = settings.phone_hmac_key in INSECURE_KEYS
    if not settings.phone_hmac_key or (insecure and settings.app_env == "prod"):
        raise HTTPException(status_code=503, detail="PHONE_HMAC_KEY no está configurada")
    queue = JobQueue(session)
    for message in parse_webhook(await request.json()):
        queue.enqueue(WHATSAPP_MESSAGE, message.__dict__)
    return {"status": "ok"}
