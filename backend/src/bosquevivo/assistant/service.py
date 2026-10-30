from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bosquevivo.ai.agent import Agent, AgentRequest
from bosquevivo.ai.agent.router import classify_query
from bosquevivo.ai.llm import LLMProvider, MediaPart
from bosquevivo.assistant import texts
from bosquevivo.assistant.client import WhatsAppClient
from bosquevivo.assistant.dialogue import (
    split_message,
    wants_consent,
    wants_deletion,
    wants_media_consent,
)
from bosquevivo.assistant.media import strip_metadata
from bosquevivo.assistant.messages import IncomingMessage
from bosquevivo.assistant.voice import transcribe_audio
from bosquevivo.core.config import Settings
from bosquevivo.core.errors import ExternalServiceError
from bosquevivo.core.logging import get_logger
from bosquevivo.core.privacy import phone_hmac
from bosquevivo.db.models import AgentRun, ConsentLog, Evidence, Job, ProcessedMessage, WaUser
from bosquevivo.jobs.kinds import WHATSAPP_MESSAGE

log = get_logger(__name__)

PURPOSE_DATA = "datos"
PURPOSE_MEDIA = "medios"


class AssistantService:
    def __init__(
        self,
        session: Session,
        agent: Agent,
        client: WhatsAppClient,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self.session = session
        self.agent = agent
        self.client = client
        self.llm = llm
        self.settings = settings

    def already_processed(self, message_id: str) -> bool:
        return self.session.get(ProcessedMessage, message_id) is not None

    def mark_processed(self, message_id: str) -> bool:
        if self.already_processed(message_id):
            return False
        self.session.add(ProcessedMessage(message_id=message_id))
        self.session.flush()
        return True

    def _reply(self, phone: str, text: str) -> None:
        for chunk in split_message(text):
            self.client.send_text(phone, chunk)

    def _granted(self, user: WaUser, purpose: str) -> bool:
        latest = self.session.scalars(
            select(ConsentLog)
            .where(ConsentLog.user_id == user.id, ConsentLog.purpose == purpose)
            .order_by(ConsentLog.id.desc())
        ).first()
        return bool(latest and latest.granted)

    def _grant(self, user: WaUser, purpose: str) -> None:
        self.session.add(
            ConsentLog(
                user_id=user.id, purpose=purpose, notice_version=texts.NOTICE_VERSION, granted=True
            )
        )
        self.session.flush()

    def _user(self, phone: str) -> tuple[WaUser, bool]:
        digest = phone_hmac(phone, self.settings.phone_hmac_key)
        user = self.session.scalars(select(WaUser).where(WaUser.phone_hmac == digest)).first()
        if user:
            return user, False
        user = WaUser(phone_hmac=digest)
        self.session.add(user)
        self.session.flush()
        return user, True

    def delete_user(self, phone: str) -> None:
        digest = phone_hmac(phone, self.settings.phone_hmac_key)
        run_ids = self.session.scalars(select(AgentRun.id).where(AgentRun.user_ref == digest)).all()
        for run_id in run_ids:
            self.session.execute(
                delete(Evidence).where(Evidence.id.like(f"r{run_id}\\_%", escape="\\"))
            )
        self.session.execute(delete(AgentRun).where(AgentRun.user_ref == digest))
        self._delete_pending_messages(phone)
        user = self.session.scalars(select(WaUser).where(WaUser.phone_hmac == digest)).first()
        if user:
            self.session.delete(user)
        self.session.flush()

    def _delete_pending_messages(self, phone: str) -> None:
        """Mensajes de la persona que siguen en la cola con su teléfono. El que se está
        procesando (running) se limpia al terminar (`jobs.queue.scrub`)."""
        jobs = self.session.scalars(
            select(Job).where(Job.kind == WHATSAPP_MESSAGE, Job.status != "running")
        ).all()
        for job in jobs:
            if job.payload.get("phone") == phone:
                self.session.delete(job)

    def handle(self, message: IncomingMessage) -> None:
        if not self.mark_processed(message.message_id):
            return
        phone = message.phone
        if wants_deletion(message.text):
            self.delete_user(phone)
            self._reply(phone, texts.DELETED)
            return
        user, created = self._user(phone)
        if created:
            self._reply(phone, texts.WELCOME)
            self._reply(phone, texts.NOTICE)
            return
        if wants_consent(message.text):
            self._grant(user, PURPOSE_DATA)
            self._reply(phone, texts.CONSENT_GRANTED)
            return
        if not self._granted(user, PURPOSE_DATA):
            self._reply(phone, texts.CONSENT_PENDING)
            return
        if wants_media_consent(message.text):
            self._grant(user, PURPOSE_MEDIA)
            self._reply(phone, texts.MEDIA_GRANTED)
            return
        self._respond(user, message)

    def _respond(self, user: WaUser, message: IncomingMessage) -> None:
        phone = message.phone
        media_ok = self._granted(user, PURPOSE_MEDIA)
        if message.kind in {"image", "audio"} and not media_ok:
            self._reply(phone, texts.MEDIA_NOTICE)
            return
        if message.kind == "other":
            self._reply(phone, texts.UNSUPPORTED)
            return
        try:
            request = self._build_request(user, message, media_ok)
        except ExternalServiceError as exc:
            log.warning("media_unavailable", error=str(exc))
            self._reply(phone, texts.FAILURE)
            return
        if request is None:
            self._reply(phone, texts.AUDIO_UNCLEAR)
            return
        if classify_query(request.text, bool(request.media)) == "profunda":
            self._reply(phone, texts.WORKING)  # acuse inmediato de la consulta profunda (ADR 0010)
        answer = self.agent.answer(request)
        self._reply(phone, answer.text)

    def _build_request(
        self, user: WaUser, message: IncomingMessage, media_ok: bool
    ) -> AgentRequest | None:
        text = message.text or ""
        location = None
        media: list[MediaPart] = []
        if (
            message.kind == "location"
            and message.latitude is not None
            and message.longitude is not None
        ):
            location = (float(message.latitude), float(message.longitude))
            text = text or "¿Qué hay en este lugar?"
        if message.kind == "audio" and message.media_id:
            data, mime = self.client.download_media(message.media_id)
            transcript = transcribe_audio(self.llm, MediaPart(data, mime))
            if transcript is None:
                return None
            text = transcript
        if message.kind == "image" and message.media_id:
            data, _ = self.client.download_media(message.media_id)
            media = [MediaPart(*strip_metadata(data))]
            text = text or "Mire esta foto"
        return AgentRequest(
            text=text,
            role="comunidad",
            user_ref=user.phone_hmac,
            location=location,
            media=media,
            media_consent=media_ok,
        )
