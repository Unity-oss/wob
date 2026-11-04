"""The worker process: document ingestion, plus claim extraction when an LLM is enabled.

Usage: python -m app.workers.main
"""

import logging
import signal
import time

from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import get_engine
from app.services.claims.llm import AIProcessingNotApproved, get_claim_llm
from app.services.reranking import get_reranker
from app.services.research.llm import get_research_llm
from app.services.research.matching import AnthropicClaimMatcher
from app.services.retrieval import Retriever
from app.workers.assessment import AnthropicAssessor, AssessmentPipeline
from app.workers.claims import ClaimPipeline
from app.workers.ingest import build_pipeline
from app.workers.research import ResearchPipeline

log = logging.getLogger("wob.worker")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    ingest = build_pipeline(settings)
    matcher = None
    try:
        claim_llm = get_claim_llm(settings)
        matcher = AnthropicClaimMatcher(claim_llm)
        claims = ClaimPipeline(
            sessionmaker(get_engine(), expire_on_commit=False), claim_llm, settings, matcher
        )
        log.info("claim extraction enabled (%s)", claims.llm.model_id)
    except AIProcessingNotApproved as exc:
        claims = None
        log.info("claim extraction off: %s", exc)

    assessment = None
    if claims is not None:
        # Reuse the ingestion embedder instead of loading a second copy of the model.
        retriever = Retriever(ingest.embedder, get_reranker(settings), settings)
        assessment = AssessmentPipeline(
            sessionmaker(get_engine(), expire_on_commit=False),
            AnthropicAssessor(claims.llm),
            retriever,
            settings,
        )
        log.info("criterion assessment enabled")

    try:
        research = ResearchPipeline(
            sessionmaker(get_engine(), expire_on_commit=False),
            get_research_llm(settings),
            settings,
            matcher,
        )
        log.info("external research enabled")
    except AIProcessingNotApproved as exc:
        research = None
        log.info("external research off: %s", exc)

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("stopping after the current job")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker ready (scanner=%s, embeddings=%s)", settings.scanner, ingest.embedder.model_id)
    while not stopping:
        # Ingestion first: claims should see every document that is ready.
        worked = (
            ingest.run_once()
            or (claims is not None and claims.run_once())
            or (research is not None and research.run_once())
            or (assessment is not None and assessment.run_once())
        )
        if not worked:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
