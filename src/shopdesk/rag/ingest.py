import argparse
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from shopdesk.config import get_settings
from shopdesk.observability import configure_logging
from shopdesk.rag.chunking import RecursiveChunker
from shopdesk.rag.embeddings import Encoder
from shopdesk.rag.loaders import NotionLoader, load_markdown_dir
from shopdesk.rag.models import KBDocument
from shopdesk.rag.store import KnowledgeStore
from shopdesk.runtime import run
from shopdesk.security.injection import scan_kb_text
from shopdesk.wiring import build_encoder, build_store

log = structlog.get_logger()


@dataclass
class IngestReport:
    indexed: int = 0
    unchanged: int = 0
    chunks: int = 0
    quarantined: list[str] = field(default_factory=list)


class Ingestor:
    def __init__(self, store: KnowledgeStore, encoder: Encoder, chunker: RecursiveChunker):
        self._store = store
        self._encoder = encoder
        self._chunker = chunker

    async def run(
        self, docs: Iterable[KBDocument] | AsyncIterable[KBDocument], force: bool = False
    ) -> IngestReport:
        await self._store.ensure_collection(self._encoder.dim)
        report = IngestReport()
        if isinstance(docs, AsyncIterable):
            async for doc in docs:
                await self._one(doc, report, force)
        else:
            for doc in docs:
                await self._one(doc, report, force)
        log.info("ingest.done", **report.__dict__)
        return report

    async def _one(self, doc: KBDocument, report: IngestReport, force: bool) -> None:
        if not force and await self._store.current_version(doc.doc_id) == doc.content_hash:
            report.unchanged += 1
            return

        chunks = []
        for chunk in self._chunker.split(doc.body):
            flags = scan_kb_text(chunk.text)
            if flags:
                log.warning("ingest.chunk_quarantined", doc_id=doc.doc_id, index=chunk.index,
                            flags=flags)
                report.quarantined.append(f"{doc.doc_id}#{chunk.index}")
                continue
            chunks.append(chunk)

        texts = [c.embedding_text(doc.title) for c in chunks]
        vectors = await self._encoder.embed_documents(texts) if texts else []
        report.chunks += await self._store.replace_document(doc, chunks, vectors)
        report.indexed += 1


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Index the help-centre into Qdrant")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--markdown", type=Path, help="directory of .md files")
    source.add_argument("--notion-database", help="Notion database id (needs NOTION_TOKEN)")
    parser.add_argument("--force", action="store_true", help="re-embed unchanged documents")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)
    ingestor = Ingestor(
        build_store(settings),
        build_encoder(settings),
        RecursiveChunker(settings.chunk_max_tokens, settings.chunk_overlap_tokens),
    )
    if args.markdown:
        if not args.markdown.is_dir():
            raise SystemExit(f"{args.markdown} is not a directory")
        report = await ingestor.run(load_markdown_dir(args.markdown), force=args.force)
    else:
        import os

        loader = NotionLoader(os.environ["NOTION_TOKEN"], args.notion_database)
        report = await ingestor.run(loader.documents(), force=args.force)
    print(
        f"indexed={report.indexed} unchanged={report.unchanged} chunks={report.chunks} "
        f"quarantined={len(report.quarantined)}"
    )


if __name__ == "__main__":
    run(_main())
