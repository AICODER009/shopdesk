"""Provider-neutral conversation log.

The graph stores messages in this shape and renders them per provider at call time, so the
primary and fallback models can take turns in one conversation. Each assistant message
keeps the provider's raw output blocks (`native`) and those are replayed verbatim to the same
provider, because reasoning/thinking blocks must round-trip unchanged. The other provider
gets the neutral text + tool calls instead.

The log is append-only. Never edit or drop earlier entries: both providers rely on a stable
prefix for prompt caching, and Anthropic rejects edited history behind thinking blocks.
"""

import json
from typing import Any, Literal, TypedDict


class ToolCall(TypedDict):
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(TypedDict):
    call_id: str
    name: str
    content: str
    is_error: bool


Provider = Literal["openai", "anthropic"]


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "text": text}


def note(text: str) -> dict[str, Any]:
    """Operator-side note: guard flags, validation feedback, handback after a human."""
    return {"role": "note", "text": text}


def assistant(
    *,
    text: str = "",
    tool_calls: list[ToolCall] | None = None,
    provider: Provider | None = None,
    native: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "text": text,
        "tool_calls": tool_calls or [],
        "provider": provider,
        "native": native or [],
    }


def tool_results(results: list[ToolResult]) -> dict[str, Any]:
    return {"role": "tool", "results": results}


def to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        match m["role"]:
            case "user":
                push("user", [{"type": "text", "text": m["text"]}])
            case "note":
                text = f"[Platform note, not written by the customer] {m['text']}"
                push("user", [{"type": "text", "text": text}])
            case "assistant" if m.get("provider") == "anthropic" and m.get("native"):
                push("assistant", m["native"])
            case "assistant":
                blocks: list[dict[str, Any]] = []
                if m["text"]:
                    blocks.append({"type": "text", "text": m["text"]})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m["tool_calls"]
                ]
                push("assistant", blocks)
            case "tool":
                push(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": r["call_id"],
                            "content": r["content"],
                            "is_error": r["is_error"],
                        }
                        for r in m["results"]
                    ],
                )
    return out


def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        match m["role"]:
            case "user":
                items.append({"role": "user", "content": m["text"]})
            case "note":
                items.append({"role": "developer", "content": m["text"]})
            case "assistant" if m.get("provider") == "openai" and m.get("native"):
                items.extend(m["native"])
            case "assistant":
                if m["text"]:
                    items.append({"role": "assistant", "content": m["text"]})
                items += [
                    {
                        "type": "function_call",
                        "call_id": c["id"],
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"]),
                    }
                    for c in m["tool_calls"]
                ]
            case "tool":
                items += [
                    {"type": "function_call_output", "call_id": r["call_id"],
                     "output": r["content"]}
                    for r in m["results"]
                ]
    return items


def customer_texts(messages: list[dict[str, Any]]) -> list[str]:
    return [m["text"] for m in messages if m["role"] == "user"]
