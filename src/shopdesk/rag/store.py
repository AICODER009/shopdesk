import asyncio
import uuid
from collections.abc import Sequence

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from shopdesk.rag.chunking import Chunk
from shopdesk.rag.embeddings import HybridVector
from shopdesk.rag.models import KBDocument, RetrievedChunk

log = structlog.get_logger()

DENSE = "dense"
SPARSE = "bm25"
_POINT_NS = uuid.UUID("6f1d4b8e-2c3a-4f5e-9a7b-1c2d3e4f5a6b")


def point_id(doc_id: str, version: str, index: int) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{doc_id}:{version}:{index}"))


def _match(key: str, value: str) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, match=qm.MatchValue(value=value))


class KnowledgeStore:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self.collection = collection

    async def ensure_collection(self, dim: int) -> None:
        if await self._client.collection_exists(self.collection):
            info = await self._client.get_collection(self.collection)
            vectors = info.config.params.vectors
            existing = vectors[DENSE].size if isinstance(vectors, dict) else None
            if existing != dim:
                # Switching embedding model/dimensions needs a new collection and a full
                # re-index; silently mixing vector spaces would wreck retrieval.
                raise RuntimeError(
                    f"collection {self.collection!r} has dense size {existing}, encoder "
                    f"produces {dim}; set QDRANT_COLLECTION to a new name and re-ingest"
                )
            return
        await self._client.create_collection(
            self.collection,
            vectors_config={DENSE: qm.VectorParams(size=dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
        )
        for field in ("doc_id", "locale", "version"):
            await self._client.create_payload_index(
                self.collection, field, qm.PayloadSchemaType.KEYWORD
            )
        log.info("qdrant.collection_created", collection=self.collection, dim=dim)

    async def current_version(self, doc_id: str) -> str | None:
        points, _ = await self._client.scroll(
            self.collection,
            scroll_filter=qm.Filter(must=[_match("doc_id", doc_id)]),
            limit=1,
            with_payload=["version"],
        )
        return points[0].payload.get("version") if points and points[0].payload else None

    async def replace_document(
        self, doc: KBDocument, chunks: Sequence[Chunk], vectors: Sequence[HybridVector]
    ) -> int:
        version = doc.content_hash
        points = [
            qm.PointStruct(
                id=point_id(doc.doc_id, version, chunk.index),
                vector={
                    DENSE: vec.dense,
                    SPARSE: qm.SparseVector(indices=vec.sparse.indices, values=vec.sparse.values),
                },
                payload={
                    "doc_id": doc.doc_id,
                    "version": version,
                    "title": doc.title,
                    "url": str(doc.url) if doc.url else None,
                    "locale": doc.locale,
                    "source": doc.source,
                    "breadcrumb": chunk.breadcrumb,
                    "chunk_index": chunk.index,
                    "text": chunk.text,
                    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
                },
            )
            for chunk, vec in zip(chunks, vectors, strict=True)
        ]
        # Write the new version first, then drop the old one, so readers never see a gap.
        if points:
            await self._client.upsert(self.collection, points, wait=True)
        await self._client.delete(
            self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[_match("doc_id", doc.doc_id)],
                    must_not=[_match("version", version)],
                )
            ),
            wait=True,
        )
        return len(points)

    async def delete_document(self, doc_id: str) -> None:
        await self._client.delete(
            self.collection,
            points_selector=qm.FilterSelector(filter=qm.Filter(must=[_match("doc_id", doc_id)])),
            wait=True,
        )

    async def hybrid_search(
        self, vector: HybridVector, *, limit: int, locale: str | None = None
    ) -> list[RetrievedChunk]:
        flt = qm.Filter(must=[_match("locale", locale)]) if locale else None
        fused_call = self._client.query_points(
            self.collection,
            prefetch=[
                qm.Prefetch(query=vector.dense, using=DENSE, limit=limit, filter=flt),
                qm.Prefetch(
                    query=qm.SparseVector(
                        indices=vector.sparse.indices, values=vector.sparse.values
                    ),
                    using=SPARSE,
                    limit=limit,
                    filter=flt,
                ),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        # RRF only ranks; the cosine from a plain dense query is the absolute similarity
        # signal the retriever gates on.
        dense_call = self._client.query_points(
            self.collection, query=vector.dense, using=DENSE, limit=limit, query_filter=flt,
            with_payload=False,
        )
        response, dense = await asyncio.gather(fused_call, dense_call)
        cosine = {str(p.id): p.score for p in dense.points}
        out: list[RetrievedChunk] = []
        for p in response.points:
            payload = p.payload or {}
            out.append(
                RetrievedChunk(
                    point_id=str(p.id),
                    doc_id=payload["doc_id"],
                    title=payload.get("title", ""),
                    url=payload.get("url"),
                    breadcrumb=payload.get("breadcrumb", ""),
                    text=payload.get("text", ""),
                    fusion_score=p.score,
                    dense_score=cosine.get(str(p.id)),
                )
            )
        return out
