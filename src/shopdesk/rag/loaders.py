from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from shopdesk.integrations.http import ensure_ok, request_with_retries
from shopdesk.rag.models import KBDocument


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[end + 4 :].lstrip("\n")


def load_markdown_dir(root: Path) -> Iterator[KBDocument]:
    for path in sorted(root.rglob("*.md")):
        meta, body = _front_matter(path.read_text(encoding="utf-8"))
        title = meta.get("title") or path.stem.replace("-", " ").title()
        yield KBDocument(
            doc_id=meta.get("id") or path.relative_to(root).with_suffix("").as_posix(),
            title=title,
            body=body,
            url=meta.get("url") or None,
            locale=meta.get("locale", "en"),
            source="markdown",
            updated_at=datetime.fromisoformat(meta["updated"]) if "updated" in meta else None,
        )


class NotionLoader:
    """Pulls help-centre pages from a Notion database and renders them to markdown."""

    API = "https://api.notion.com/v1"
    VERSION = "2022-06-28"

    def __init__(self, token: str, database_id: str, client: httpx.AsyncClient | None = None):
        self._database_id = database_id
        self._client = client or httpx.AsyncClient(timeout=30)
        self._headers = {"Authorization": f"Bearer {token}", "Notion-Version": self.VERSION}

    async def documents(self) -> AsyncIterator[KBDocument]:
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 50}
            if cursor:
                body["start_cursor"] = cursor
            # Notion's query endpoint is a read despite being POST, so retrying it is safe.
            resp = await request_with_retries(
                self._client,
                "POST",
                f"{self.API}/databases/{self._database_id}/query",
                json=body,
                headers=self._headers,
                retry_unsafe=True,
            )
            data = ensure_ok(resp, "notion").json()
            for page in data["results"]:
                if page.get("archived"):
                    continue
                yield KBDocument(
                    doc_id=f"notion:{page['id']}",
                    title=self._title(page),
                    body=await self._render(page["id"]),
                    url=page.get("public_url") or page.get("url"),
                    source="notion",
                    updated_at=datetime.fromisoformat(page["last_edited_time"]),
                )
            if not data.get("has_more"):
                return
            cursor = data["next_cursor"]

    @staticmethod
    def _title(page: dict[str, Any]) -> str:
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                return "".join(t["plain_text"] for t in prop["title"]) or "Untitled"
        return "Untitled"

    async def _children(self, block_id: str) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            resp = await request_with_retries(
                self._client,
                "GET",
                f"{self.API}/blocks/{block_id}/children",
                params=params,
                headers=self._headers,
            )
            data = ensure_ok(resp, "notion").json()
            for block in data["results"]:
                yield block
            if not data.get("has_more"):
                return
            cursor = data["next_cursor"]

    async def _render(self, block_id: str, depth: int = 0) -> str:
        lines: list[str] = []
        indent = "  " * depth
        async for block in self._children(block_id):
            kind = block["type"]
            payload = block.get(kind, {})
            text = "".join(t.get("plain_text", "") for t in payload.get("rich_text", []))
            match kind:
                case "heading_1" | "heading_2" | "heading_3":
                    lines.append(f"\n{'#' * (int(kind[-1]) + 1)} {text}\n")
                case "bulleted_list_item":
                    lines.append(f"{indent}- {text}")
                case "numbered_list_item":
                    lines.append(f"{indent}1. {text}")
                case "to_do":
                    lines.append(f"{indent}- [{'x' if payload.get('checked') else ' '}] {text}")
                case "quote" | "callout":
                    lines.append(f"> {text}")
                case "code":
                    lines.append(f"```\n{text}\n```")
                case "paragraph":
                    lines.append(f"{text}\n")
                case _:
                    pass
            if block.get("has_children") and kind not in ("child_page", "child_database"):
                lines.append(await self._render(block["id"], depth + 1))
        return "\n".join(lines)
