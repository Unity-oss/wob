"""Hybrid, candidate-scoped evidence retrieval (spec §5.3, FR-05).

    candidate filter ─┬─ vector search (exact cosine, top 20) ─┐
                      └─ keyword search (full text, top 20) ───┴─ RRF fusion ─ rerank ─ top k

Isolation: the finalist-category filter is applied inside both searches, before any ranking,
and every returned passage is re-checked against it. Retrieval only reads the
knowledge_base_chunks view, so withdrawn and unprocessed documents are never returned.
"""

import uuid
from dataclasses import asdict, dataclass, field
from threading import Lock

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.services.embeddings import Embedder, get_embedder
from app.services.reranking import Reranker, get_reranker
from app.services.tracing import trace_job

RETRIEVAL_VERSION = "retrieve-v1"


class IsolationError(RuntimeError):
    """A passage from another finalist reached the result set. Should be impossible."""


@dataclass
class Passage:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    finalist_category_id: uuid.UUID
    filename: str
    source_type: str
    chunk_index: int
    page_start: int | None
    page_end: int | None
    section: str | None
    content: str
    token_count: int | None
    vector_rank: int | None = None
    lexical_rank: int | None = None
    fused_score: float = 0.0
    rerank_score: float | None = None
    rank: int = 0


@dataclass
class RetrievalResult:
    query: str
    finalist_category_id: uuid.UUID
    passages: list[Passage]
    flags: list[str]
    candidates: dict[str, int]
    config: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# Exact search: the MATERIALIZED CTE stops the planner from using the HNSW index, which
# applies the candidate filter after an approximate scan and can silently return fewer than
# n rows. A finalist has at most a few hundred chunks, so an exact scan is cheap.
_VECTOR_SQL = text(
    """
    WITH candidate AS MATERIALIZED (
        SELECT id, embedding FROM knowledge_base_chunks
        WHERE finalist_category_id = :fc AND embedding_model = :model AND embedding IS NOT NULL
    )
    SELECT id FROM candidate
    ORDER BY embedding <=> CAST(:query_vector AS vector), id
    LIMIT :n
    """
)

# plainto_tsquery ANDs every term, which almost never matches a long criterion description;
# OR the lexemes instead and let cover-density ranking reward passages matching more of them.
_LEXICAL_SQL = text(
    """
    WITH q AS (
        SELECT CASE WHEN numnode(p) = 0 THEN NULL
                    ELSE replace(p::text, ' & ', ' | ')::tsquery END AS query
        FROM plainto_tsquery('english', :query_text) AS p
    )
    SELECT c.id FROM knowledge_base_chunks c, q
    WHERE c.finalist_category_id = :fc AND q.query IS NOT NULL AND c.content_tsv @@ q.query
    ORDER BY ts_rank_cd(c.content_tsv, q.query, 1) DESC, c.id
    LIMIT :n
    """
)

_FETCH_SQL = text(
    """
    SELECT id, document_id, finalist_category_id, filename, source_type, chunk_index,
           page_number, metadata->>'page_end' AS page_end, section, content, token_count
    FROM knowledge_base_chunks
    WHERE finalist_category_id = :fc AND id = ANY(:ids)
    """
)

_COVERAGE_SQL = text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE embedding_model = :model AND embedding IS NOT NULL) AS current
    FROM knowledge_base_chunks WHERE finalist_category_id = :fc
    """
)


def criterion_query(name: str, description: str, evidence_expectations: str | None) -> str:
    """The retrieval query for one rubric criterion."""
    parts = [f"{name}.", description.strip()]
    if evidence_expectations:
        parts.append(f"Evidence: {evidence_expectations.strip()}")
    return " ".join(parts)


class Retriever:
    def __init__(self, embedder: Embedder, reranker: Reranker | None, settings: Settings) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self.settings = settings

    def search_config(self) -> dict[str, object]:
        """The settings that determine results, recorded with every assessment run."""
        s = self.settings
        return {
            "retrieval_version": RETRIEVAL_VERSION,
            "embedding_model": self.embedder.model_id,
            "reranker_model": self.reranker.model_id if self.reranker else None,
            "vector_candidates": s.retrieval_vector_candidates,
            "lexical_candidates": s.retrieval_lexical_candidates,
            "rrf_k": s.retrieval_rrf_k,
            "top_k": s.retrieval_top_k,
        }

    @trace_job("retrieval", "finalist_category_id", run_type="retriever")
    def search(
        self,
        session: Session,
        finalist_category_id: uuid.UUID,
        query: str,
        top_k: int | None = None,
    ) -> RetrievalResult:
        s = self.settings
        top_k = top_k or s.retrieval_top_k
        fc = finalist_category_id
        model = self.embedder.model_id

        vector_ids = list(
            session.execute(
                _VECTOR_SQL,
                {
                    "fc": fc,
                    "model": model,
                    "query_vector": str(self.embedder.embed_query(query)),
                    "n": s.retrieval_vector_candidates,
                },
            ).scalars()
        )
        lexical_ids = list(
            session.execute(
                _LEXICAL_SQL, {"fc": fc, "query_text": query, "n": s.retrieval_lexical_candidates}
            ).scalars()
        )

        # Reciprocal rank fusion (Cormack et al., 2009).
        fused: dict[uuid.UUID, float] = {}
        for ranking in (vector_ids, lexical_ids):
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (s.retrieval_rrf_k + rank)

        rows = session.execute(_FETCH_SQL, {"fc": fc, "ids": list(fused)}).mappings()
        passages = {
            row["id"]: Passage(
                chunk_id=row["id"],
                document_id=row["document_id"],
                finalist_category_id=row["finalist_category_id"],
                filename=row["filename"],
                source_type=row["source_type"],
                chunk_index=row["chunk_index"],
                page_start=row["page_number"],
                page_end=int(row["page_end"]) if row["page_end"] else row["page_number"],
                section=row["section"],
                content=row["content"],
                token_count=row["token_count"],
                vector_rank=vector_ids.index(row["id"]) + 1 if row["id"] in vector_ids else None,
                lexical_rank=lexical_ids.index(row["id"]) + 1 if row["id"] in lexical_ids else None,
                fused_score=fused[row["id"]],
            )
            for row in rows
        }
        ordered = sorted(passages.values(), key=lambda p: (-p.fused_score, str(p.chunk_id)))

        if self.reranker is not None and ordered:
            scores = self.reranker.rerank(query, [p.content for p in ordered])
            for passage, score in zip(ordered, scores, strict=True):
                passage.rerank_score = score
            ordered.sort(key=lambda p: (-(p.rerank_score or 0.0), -p.fused_score, str(p.chunk_id)))

        selected = ordered[:top_k]
        for rank, passage in enumerate(selected, start=1):
            passage.rank = rank
            if passage.finalist_category_id != finalist_category_id:
                raise IsolationError(f"passage {passage.chunk_id} is not from {fc}")

        coverage = session.execute(_COVERAGE_SQL, {"fc": fc, "model": model}).mappings().one()
        flags = self._flags(selected, vector_ids, lexical_ids, coverage)
        return RetrievalResult(
            query=query,
            finalist_category_id=finalist_category_id,
            passages=selected,
            flags=flags,
            candidates={
                "indexed_chunks": coverage["total"],
                "vector": len(vector_ids),
                "lexical": len(lexical_ids),
                "fused": len(ordered),
            },
            config={
                "retrieval_version": RETRIEVAL_VERSION,
                "embedding_model": model,
                "reranker_model": self.reranker.model_id if self.reranker else None,
                "vector_candidates": s.retrieval_vector_candidates,
                "lexical_candidates": s.retrieval_lexical_candidates,
                "rrf_k": s.retrieval_rrf_k,
                "top_k": top_k,
            },
        )

    def _flags(self, selected, vector_ids, lexical_ids, coverage) -> list[str]:
        """Signals of uncertain retrieval for the assessor and reviewers (spec §7.1)."""
        flags: list[str] = []
        if coverage["total"] == 0:
            flags.append("no_indexed_evidence")
        elif coverage["current"] < coverage["total"]:
            flags.append("some_evidence_not_embedded_with_current_model")
        if coverage["total"] and not selected:
            flags.append("no_matching_passages")
        elif 0 < len(selected) < 3:
            flags.append("few_passages")
        if coverage["total"] and not lexical_ids:
            flags.append("no_keyword_matches")
        threshold = self.settings.rerank_min_score
        if threshold is not None and selected and selected[0].rerank_score is not None:
            if selected[0].rerank_score < threshold:
                flags.append("low_relevance")
        return flags


_cache: dict[tuple, Retriever] = {}
_cache_lock = Lock()


def get_retriever(settings: Settings) -> Retriever:
    """One retriever (and loaded models) per provider configuration, shared across requests."""
    key = (
        settings.embedding_provider,
        settings.embedding_model,
        settings.embedding_dim,
        settings.reranker_provider,
        settings.reranker_model,
        settings.retrieval_vector_candidates,
        settings.retrieval_lexical_candidates,
        settings.retrieval_rrf_k,
        settings.retrieval_top_k,
        settings.rerank_min_score,
    )
    with _cache_lock:
        if key not in _cache:
            _cache[key] = Retriever(get_embedder(settings), get_reranker(settings), settings)
        return _cache[key]
