import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anthropic
import openai
from langfuse import get_client

from shopdesk.agent import transcript
from shopdesk.agent.transcript import Provider, ToolCall

ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"

Stop = Literal["final", "tool_calls", "refusal", "truncated"]


class ModelUnavailable(Exception):
    """Provider call failed. `transient` decides whether LangGraph retries the node."""

    def __init__(self, provider: str, reason: str, *, transient: bool) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.transient = transient


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]


@dataclass(slots=True)
class ModelTurn:
    provider: Provider
    model: str
    stop: Stop
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    native: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        return transcript.assistant(
            text=self.text, tool_calls=self.tool_calls, provider=self.provider, native=self.native
        )


class AgentModel(Protocol):
    provider: Provider
    model: str

    async def respond(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        output_schema: dict[str, Any],
        user_ref: str | None = None,
    ) -> ModelTurn: ...


def _traced(name: str, model: str, provider: str, messages: list[dict[str, Any]]) -> Any:
    return get_client().start_as_current_observation(
        as_type="generation",
        name=name,
        model=model,
        input=messages[-4:],
        metadata={"provider": provider},
    )


class OpenAIAgentModel:
    provider: Provider = "openai"

    def __init__(self, client: openai.AsyncOpenAI, model: str, effort: str = "medium") -> None:
        self._client = client
        self.model = model
        self._effort = effort

    async def respond(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        output_schema: dict[str, Any],
        user_ref: str | None = None,
    ) -> ModelTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": transcript.to_openai(messages),
            "tools": [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema,
                    "strict": True,
                }
                for t in tools
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "customer_reply",
                    "schema": output_schema,
                    "strict": True,
                }
            },
            "reasoning": {"effort": self._effort},
            # Stateless: OpenAI keeps nothing between calls, we resend the (append-only) log
            # and carry reasoning forward as encrypted blobs.
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "shopdesk-agent",
            "max_output_tokens": 16000,
        }
        if user_ref:
            kwargs["safety_identifier"] = user_ref

        with _traced("agent.llm", self.model, "openai", messages) as gen:
            try:
                resp = await self._client.responses.create(**kwargs)
            except (openai.APIConnectionError, openai.RateLimitError,
                    openai.InternalServerError) as exc:
                raise ModelUnavailable("openai", type(exc).__name__, transient=True) from exc
            except openai.APIError as exc:
                raise ModelUnavailable("openai", type(exc).__name__, transient=False) from exc

            native = [item.model_dump(mode="json", exclude_none=True) for item in resp.output]
            calls: list[ToolCall] = []
            refused = False
            for item in resp.output:
                if item.type == "function_call":
                    calls.append(
                        {"id": item.call_id, "name": item.name,
                         "arguments": json.loads(item.arguments or "{}")}
                    )
                elif item.type == "message":
                    refused = refused or any(c.type == "refusal" for c in item.content)

            if refused:
                stop: Stop = "refusal"
            elif calls:
                stop = "tool_calls"
            elif resp.status == "incomplete":
                stop = "truncated"
            else:
                stop = "final"
            usage = {}
            if resp.usage:
                usage = {
                    "input": resp.usage.input_tokens,
                    "output": resp.usage.output_tokens,
                    "cache_read_input_tokens": resp.usage.input_tokens_details.cached_tokens,
                }
            gen.update(output={"text": resp.output_text, "tool_calls": calls},
                       usage_details=usage, metadata={"stop": stop})
        return ModelTurn("openai", self.model, stop, resp.output_text, calls, native, usage)


class ClaudeAgentModel:
    provider: Provider = "anthropic"

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        effort: str = "medium",
        refusal_fallback: bool = True,
    ) -> None:
        self._client = client
        self.model = model
        self._effort = effort
        self._refusal_fallback = refusal_fallback

    async def respond(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        output_schema: dict[str, Any],
        user_ref: str | None = None,
    ) -> ModelTurn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": [
                {"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": transcript.to_anthropic(messages),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.schema,
                 "strict": True}
                for t in tools
            ],
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": output_schema},
            },
        }
        if self._refusal_fallback:
            kwargs |= {"betas": [ANTHROPIC_FALLBACK_BETA], "fallbacks": "default"}

        with _traced("agent.llm", self.model, "anthropic", messages) as gen:
            try:
                resp = await self._client.beta.messages.create(**kwargs)
            except (anthropic.APIConnectionError, anthropic.RateLimitError,
                    anthropic.InternalServerError) as exc:
                raise ModelUnavailable("anthropic", type(exc).__name__, transient=True) from exc
            except anthropic.APIError as exc:
                raise ModelUnavailable("anthropic", type(exc).__name__, transient=False) from exc

            native = [
                b.model_dump(mode="json", by_alias=True, exclude_none=True) for b in resp.content
            ]
            calls: list[ToolCall] = [
                {"id": b["id"], "name": b["name"], "arguments": b["input"]}
                for b in native
                if b["type"] == "tool_use"
            ]
            text = "".join(b.get("text", "") for b in native if b["type"] == "text")
            stop: Stop = {
                "refusal": "refusal",
                "max_tokens": "truncated",
                "tool_use": "tool_calls",
            }.get(resp.stop_reason or "", "final")  # type: ignore[assignment]
            usage = {
                "input": resp.usage.input_tokens,
                "output": resp.usage.output_tokens,
                "cache_read_input_tokens": resp.usage.cache_read_input_tokens or 0,
            }
            gen.update(output={"text": text, "tool_calls": calls}, usage_details=usage,
                       metadata={"stop": stop, "served_by": resp.model})
        if stop == "refusal":
            # A refused turn may carry partial content; don't replay it.
            native = []
        return ModelTurn("anthropic", self.model, stop, text, calls, native, usage)
