import asyncio

from langfuse import observe

from shopdesk.rag.embeddings import Encoder, Reranker
from shopdesk.rag.models import RetrievalResult
from shopdesk.rag.store import KnowledgeStore


class Retriever:
    """Hybrid recall (dense + BM25, RRF-fused), cross-encoder ordering, similarity floor.

    Whether the KB actually answers a question is a semantic call, and in evals no single
    score separated "covered" from "not covered" (a terse "return window for jumpers" scored
    below "can I pay with bitcoin", which the payments article does answer). So retrieval
    only drops clear junk via a dense-similarity floor; the model decides coverage, and
    citation checks hold it to the sources it was given.
    """

    def __init__(
        self,
        store: KnowledgeStore,
        encoder: Encoder,
        reranker: Reranker | None,
        *,
        candidates: int = 24,
        top_k: int = 5,
        min_similarity: float = 0.2,
    ) -> None:
        self._store = store
        self._encoder = encoder
        self._reranker = reranker
        self._candidates = candidates
        self._top_k = top_k
        self._min_similarity = min_similarity

    @observe(name="kb.retrieve", as_type="retriever")
    async def search(self, query: str, locale: str | None = None) -> RetrievalResult:
        vector = await self._encoder.embed_query(query)
        hits = await self._store.hybrid_search(vector, limit=self._candidates, locale=locale)
        # Sparse-only hits have no cosine; they matched on exact terms (SKUs, product
        # names), so they stay.
        hits = [h for h in hits if h.dense_score is None or h.dense_score >= self._min_similarity]
        if not hits:
            return RetrievalResult(query=query)

        if self._reranker is not None:
            docs = [f"{h.title}. {h.breadcrumb}\n{h.text}" for h in hits]
            scores = await asyncio.to_thread(self._reranker.score, query, docs)
            for hit, score in zip(hits, scores, strict=True):
                hit.rerank_score = score
            hits.sort(key=lambda h: h.rerank_score or 0.0, reverse=True)

        best = max((h.dense_score for h in hits if h.dense_score is not None), default=None)
        return RetrievalResult(query=query, chunks=hits[: self._top_k], best_score=best)
