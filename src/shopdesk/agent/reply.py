import re
from collections.abc import Mapping
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from shopdesk.agent.state import Citation

TOOL_NAMES = (
    "search_knowledge_base",
    "get_order",
    "list_recent_orders",
    "get_account_summary",
    "handoff_to_human",
)
# Bracketed only: bare "S23" is far more likely a product name than a leaked source id.
_SOURCE_REF = re.compile(r"[\[(]S\d{1,3}[\])]")


class ReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[str]
    grounded_in: Literal["knowledge_base", "account_data", "both", "none"]
    confidence: Literal["high", "medium", "low"]


REPLY_SCHEMA: dict[str, Any] = anthropic.transform_schema(ReplyDraft)


def parse_reply(text: str) -> ReplyDraft | None:
    try:
        return ReplyDraft.model_validate_json(text)
    except ValidationError:
        return None


def check_reply(
    draft: ReplyDraft, sources: Mapping[str, Mapping[str, Any]], canary: str
) -> list[str]:
    problems: list[str] = []
    answer = draft.answer.strip()
    if not answer:
        problems.append("The answer is empty.")
    unknown = [c for c in draft.citations if c not in sources]
    if unknown:
        problems.append(
            f"Citations {unknown} don't match any source returned in this conversation. "
            "Only cite ids that appear in search results."
        )
    if draft.grounded_in in ("knowledge_base", "both") and not draft.citations:
        problems.append("The answer relies on the knowledge base but cites no sources.")
    if canary in answer:
        problems.append("The answer contains the internal reference. Remove it.")
    if any(name in answer for name in TOOL_NAMES):
        problems.append("The answer mentions internal tool names.")
    if _SOURCE_REF.search(answer):
        problems.append("Put source ids in `citations`, not in the answer text.")
    return problems


def citations_for(draft: ReplyDraft, sources: Mapping[str, Mapping[str, Any]]) -> list[Citation]:
    seen: set[str] = set()
    out: list[Citation] = []
    for sid in draft.citations:
        src = sources.get(sid)
        if not src or src["doc_id"] in seen:
            continue
        seen.add(src["doc_id"])
        out.append(Citation(title=src["title"], url=src.get("url")))
    return out
