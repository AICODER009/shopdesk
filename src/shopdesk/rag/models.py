import hashlib
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class KBDocument(BaseModel):
    doc_id: str
    title: str
    body: str
    url: HttpUrl | None = None
    locale: str = "en"
    source: str = "markdown"
    updated_at: datetime | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.title}\n{self.body}".encode()).hexdigest()[:16]


class RetrievedChunk(BaseModel):
    point_id: str
    doc_id: str
    title: str
    url: str | None = None
    breadcrumb: str = ""
    text: str
    fusion_score: float = 0.0
    dense_score: float | None = None
    rerank_score: float | None = None


class RetrievalResult(BaseModel):
    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    best_score: float | None = None

    @property
    def empty(self) -> bool:
        return not self.chunks
