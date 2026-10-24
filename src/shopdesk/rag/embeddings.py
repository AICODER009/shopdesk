import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

import openai


@dataclass(frozen=True, slots=True)
class SparseVec:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True, slots=True)
class HybridVector:
    dense: list[float]
    sparse: SparseVec


class Encoder(Protocol):
    @property
    def dim(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[HybridVector]: ...

    async def embed_query(self, text: str) -> HybridVector: ...


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]: ...


class DenseEncoder(Protocol):
    @property
    def dim(self) -> int: ...

    async def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]: ...


class OpenAIDenseEncoder:
    def __init__(self, client: openai.AsyncOpenAI, model: str, dimensions: int,
                 batch_size: int = 128) -> None:
        self._client = client
        self._model = model
        self.dim = dimensions
        self._batch = batch_size

    async def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            resp = await self._client.embeddings.create(
                model=self._model, input=list(texts[i : i + self._batch]), dimensions=self.dim
            )
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return out


class FastEmbedDenseEncoder:
    """Local alternative: nothing leaves the box, at some cost in retrieval quality."""

    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        self._name = model
        self._cache_dir = cache_dir

    @cached_property
    def _model(self):  # type: ignore[no-untyped-def]
        from fastembed import TextEmbedding

        return TextEmbedding(self._name, cache_dir=self._cache_dir)

    @cached_property
    def dim(self) -> int:
        return len(next(iter(self._model.embed(["dimension probe"]))))

    async def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        fn = self._model.query_embed if query else self._model.passage_embed
        return await asyncio.to_thread(lambda: [v.tolist() for v in fn(list(texts))])


class Bm25SparseEncoder:
    def __init__(self, model: str = "Qdrant/bm25", cache_dir: str | None = None) -> None:
        self._name = model
        self._cache_dir = cache_dir

    @cached_property
    def _model(self):  # type: ignore[no-untyped-def]
        from fastembed import SparseTextEmbedding

        return SparseTextEmbedding(self._name, cache_dir=self._cache_dir)

    async def embed(self, texts: Sequence[str], *, query: bool) -> list[SparseVec]:
        fn = self._model.query_embed if query else self._model.passage_embed

        def run() -> list[SparseVec]:
            return [SparseVec(s.indices.tolist(), s.values.tolist()) for s in fn(list(texts))]

        return await asyncio.to_thread(run)


class HybridEncoder:
    def __init__(self, dense: DenseEncoder, sparse: Bm25SparseEncoder) -> None:
        self._dense = dense
        self._sparse = sparse

    @property
    def dim(self) -> int:
        return self._dense.dim

    async def embed_documents(self, texts: Sequence[str]) -> list[HybridVector]:
        dense, sparse = await asyncio.gather(
            self._dense.embed(texts, query=False), self._sparse.embed(texts, query=False)
        )
        return [HybridVector(d, s) for d, s in zip(dense, sparse, strict=True)]

    async def embed_query(self, text: str) -> HybridVector:
        dense, sparse = await asyncio.gather(
            self._dense.embed([text], query=True), self._sparse.embed([text], query=True)
        )
        return HybridVector(dense[0], sparse[0])


class CrossEncoderReranker:
    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        self._name = model
        self._cache_dir = cache_dir

    @cached_property
    def _model(self):  # type: ignore[no-untyped-def]
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        return TextCrossEncoder(self._name, cache_dir=self._cache_dir)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, list(documents))]
