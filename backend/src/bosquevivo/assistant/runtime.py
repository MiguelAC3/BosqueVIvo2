from sqlalchemy.orm import Session

from bosquevivo.ai.agent import Agent, default_registry
from bosquevivo.ai.llm import GeminiProvider, LLMProvider
from bosquevivo.ai.llm.cache import ResponseCache
from bosquevivo.ai.llm.limits import RateLimiter
from bosquevivo.assistant.client import CloudApiClient, WhatsAppClient
from bosquevivo.assistant.service import AssistantService
from bosquevivo.core.config import Settings
from bosquevivo.satellite.provider import SatelliteProvider
from bosquevivo.satellite.service import SatelliteService

_shared_limiter: RateLimiter | None = None


def shared_limiter(settings: Settings) -> RateLimiter:
    """Un solo contador por proceso: el chat del panel y cada mensaje de WhatsApp comparten
    la cuota del modelo."""
    global _shared_limiter  # noqa: PLW0603
    if _shared_limiter is None:
        _shared_limiter = RateLimiter(
            settings.gemini_requests_per_minute,
            settings.gemini_requests_per_day,
            max_wait=settings.gemini_max_wait_seconds,
        )
    return _shared_limiter


def build_llm(settings: Settings) -> LLMProvider:
    return GeminiProvider(
        settings.gemini_api_key,
        settings.gemini_model,
        light_model=settings.gemini_model_light or None,
        limiter=shared_limiter(settings),
        cache=ResponseCache(settings.cache_dir / "llm"),
    )


def build_assistant(
    session: Session,
    settings: Settings,
    llm: LLMProvider,
    client: WhatsAppClient | None = None,
    satellite_provider: SatelliteProvider | None = None,
) -> AssistantService:
    satellite = (
        SatelliteService(session, satellite_provider, settings.images_dir)
        if satellite_provider
        else None
    )
    agent = Agent(session, llm, default_registry(), settings, satellite)
    resolved = client or CloudApiClient(settings.whatsapp_token, settings.whatsapp_phone_number_id)
    return AssistantService(session, agent, resolved, llm, settings)
