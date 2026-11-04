import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.api import (
    ai_costs,
    assessment,
    auth,
    calibration,
    claims,
    dashboard,
    data_protection,
    health,
    judges_pack,
    judging,
    knowledge_base,
    reports,
    research,
)
from app.config import get_settings
from app.services.retrieval import get_retriever

log = logging.getLogger("wob.api")


def _warm_models() -> None:
    """Load (and on first run, download) the embedding and reranking models in the background,
    so the first search doesn't stall for a minute."""
    try:
        get_retriever(get_settings())
        log.info("retrieval models ready")
    except Exception:
        log.exception("could not load retrieval models; search will retry on first use")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().embedding_provider in ("local", "voyage"):
        threading.Thread(target=_warm_models, name="warm-models", daemon=True).start()
    yield


app = FastAPI(
    title="WOB Awards 2026 – AI RAG Finalists Ranking",
    version="0.1.0",
    description=(
        "AI rankings are advisory. The awards committee retains responsibility for final "
        "deliberation and winner approval."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def no_store(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """API responses hold personal data, so neither the browser nor a proxy may keep a copy
    to show after signing out."""
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(health.router)
app.include_router(ai_costs.router)
app.include_router(auth.router)
app.include_router(knowledge_base.router)
app.include_router(claims.router)
app.include_router(research.router)
app.include_router(assessment.router)
app.include_router(reports.router)
app.include_router(judges_pack.router)
app.include_router(calibration.router)
app.include_router(data_protection.router)
app.include_router(dashboard.router)
app.include_router(judging.router)
