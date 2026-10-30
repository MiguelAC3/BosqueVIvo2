from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bosquevivo import __version__
from bosquevivo.ai.knowledge import KnowledgeBase
from bosquevivo.api.routers import admin, audits, chat, health, projects, whatsapp
from bosquevivo.core.config import get_settings
from bosquevivo.core.errors import ExternalServiceError, NotFoundError, ValidationFailedError
from bosquevivo.core.logging import configure_logging, get_logger
from bosquevivo.db.session import get_engine, get_session, init_db, make_session_factory
from bosquevivo.jobs import JobWorker
from bosquevivo.jobs.kinds import CALCULATE_AUDIT, GENERATE_REPORT, WHATSAPP_MESSAGE
from bosquevivo.jobs.tasks import (
    generate_report,
    make_calculate_audit,
    make_whatsapp_message,
)
from bosquevivo.satellite.factory import build_provider

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    init_db()
    with get_session() as session:
        # el corpus es pequeño: se recarga en cada arranque para seguir los cambios del repo
        log.info("knowledge_loaded", chunks=KnowledgeBase(session).load_directory())
    worker = JobWorker(make_session_factory(get_engine()))
    worker.register(GENERATE_REPORT, generate_report)
    provider = None
    try:
        provider = build_provider(settings)
        worker.register(CALCULATE_AUDIT, make_calculate_audit(provider))
    except Exception as exc:
        log.warning("satellite_provider_unavailable", error=str(exc))
    worker.register(WHATSAPP_MESSAGE, make_whatsapp_message(provider))
    worker.start()
    app.state.worker = worker
    try:
        yield
    finally:
        worker.stop()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Bosque Vivo", version=__version__, lifespan=lifespan)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationFailedError)
    async def validation_handler(_: Request, exc: ValidationFailedError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ExternalServiceError)
    async def external_handler(_: Request, exc: ExternalServiceError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    app.include_router(health.router)
    app.include_router(projects.router)
    app.include_router(audits.router)
    app.include_router(admin.router)
    app.include_router(chat.router)
    app.include_router(whatsapp.router)
    return app


app = create_app()
