from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import openai
import pytest
import respx

from shopdesk.rag.embeddings import OpenAIDenseEncoder
from shopdesk.rag.loaders import NotionLoader
from shopdesk.security.injection import (
    AnthropicClassifier,
    ClassifierError,
    InjectionVerdict,
    OpenAIClassifier,
)

NOTION = "https://api.notion.com/v1"


def rt(text: str) -> dict[str, Any]:
    return {"rich_text": [{"plain_text": text}]}


@respx.mock
async def test_notion_loader_paginates_and_renders_markdown():
    respx.post(f"{NOTION}/databases/db1/query").mock(side_effect=[
        httpx.Response(200, json={"results": [{
            "id": "p1", "archived": False, "url": "https://notion.so/p1",
            "last_edited_time": "2026-10-01T10:00:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Returns"}]}},
        }], "has_more": True, "next_cursor": "c2"}),
        httpx.Response(200, json={"results": [{"id": "p2", "archived": True}],
                                  "has_more": False}),
    ])
    respx.get(f"{NOTION}/blocks/p1/children").mock(return_value=httpx.Response(200, json={
        "results": [
            {"id": "b1", "type": "heading_2", "heading_2": rt("Time limits")},
            {"id": "b2", "type": "paragraph", "paragraph": rt("30 days.")},
            {"id": "b3", "type": "bulleted_list_item", "bulleted_list_item": rt("tags on"),
             "has_children": True},
            {"id": "b4", "type": "to_do", "to_do": rt("print label") | {"checked": True}},
            {"id": "b5", "type": "code", "code": rt("x = 1")},
            {"id": "b6", "type": "quote", "quote": rt("note")},
            {"id": "b7", "type": "numbered_list_item", "numbered_list_item": rt("step")},
            {"id": "b8", "type": "image", "image": {}},
        ],
        "has_more": False,
    }))
    respx.get(f"{NOTION}/blocks/b3/children").mock(return_value=httpx.Response(200, json={
        "results": [{"id": "b9", "type": "bulleted_list_item",
                     "bulleted_list_item": rt("unworn")}],
        "has_more": False,
    }))

    async with httpx.AsyncClient() as client:
        docs = [d async for d in NotionLoader("secret", "db1", client).documents()]

    assert len(docs) == 1
    doc = docs[0]
    assert (doc.doc_id, doc.title, doc.source) == ("notion:p1", "Returns", "notion")
    assert "### Time limits" in doc.body
    assert "- tags on" in doc.body and "  - unworn" in doc.body
    assert "- [x] print label" in doc.body and "```\nx = 1\n```" in doc.body


async def test_openai_embeddings_are_batched_and_reordered():
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        data = [SimpleNamespace(index=i, embedding=[float(len(t))])
                for i, t in enumerate(kwargs["input"])]
        return SimpleNamespace(data=list(reversed(data)))

    client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    enc = OpenAIDenseEncoder(client, "text-embedding-3-large", 256, batch_size=2)  # type: ignore[arg-type]
    vectors = await enc.embed(["a", "bb", "ccc"], query=False)
    assert vectors == [[1.0], [2.0], [3.0]]
    assert [len(c["input"]) for c in calls] == [2, 1]
    assert calls[0]["dimensions"] == 256


async def test_openai_classifier():
    verdict = InjectionVerdict(label="attack", reason="r")

    async def parse(**kwargs: Any) -> Any:
        assert kwargs["store"] is False and "&lt;b&gt;" in kwargs["input"]
        return SimpleNamespace(output_parsed=verdict)

    clf = OpenAIClassifier(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "m")  # type: ignore[arg-type]
    assert await clf.classify("<b>hi</b>") == verdict


async def test_openai_classifier_wraps_errors():
    async def parse(**kwargs: Any) -> Any:
        raise openai.APIConnectionError(request=httpx2.Request("POST", "https://x"))

    clf = OpenAIClassifier(SimpleNamespace(responses=SimpleNamespace(parse=parse)), "m")  # type: ignore[arg-type]
    with pytest.raises(ClassifierError):
        await clf.classify("hi")


@pytest.mark.parametrize(("stop", "expected"), [("end_turn", "benign"), ("refusal", None)])
async def test_anthropic_classifier(stop, expected):
    verdict = InjectionVerdict(label="benign", reason="r")

    async def parse(**kwargs: Any) -> Any:
        return SimpleNamespace(stop_reason=stop, parsed_output=verdict)

    clf = AnthropicClassifier(SimpleNamespace(messages=SimpleNamespace(parse=parse)), "m")  # type: ignore[arg-type]
    result = await clf.classify("hi")
    assert (result.label if result else None) == expected
