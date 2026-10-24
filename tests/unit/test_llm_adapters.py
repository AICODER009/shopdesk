from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic.types.beta import BetaTextBlock, BetaThinkingBlock, BetaToolUseBlock
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseReasoningItem,
)

from shopdesk.agent import transcript
from shopdesk.agent.llm import (
    ANTHROPIC_FALLBACK_BETA,
    ClaudeAgentModel,
    ModelUnavailable,
    OpenAIAgentModel,
)
from shopdesk.agent.reply import REPLY_SCHEMA
from shopdesk.agent.tools import TOOL_SPECS

REQUEST = {
    "instructions": "be helpful",
    "messages": [transcript.user("where is my order?")],
    "tools": TOOL_SPECS,
    "output_schema": REPLY_SCHEMA,
    "user_ref": "u-123",
}


class Recorder:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def openai_response(output: list[Any], text: str = "", status: str = "completed") -> Any:
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            input_tokens_details=SimpleNamespace(cached_tokens=80))
    return SimpleNamespace(output=output, output_text=text, status=status, usage=usage)


def openai_model(result: Any) -> tuple[OpenAIAgentModel, Recorder]:
    rec = Recorder(result)
    client = SimpleNamespace(responses=rec)
    return OpenAIAgentModel(client, "gpt-test", "medium"), rec  # type: ignore[arg-type]


async def test_openai_request_is_strict_stateless_and_pseudonymous():
    model, rec = openai_model(openai_response([], text='{"answer":"hi"}'))
    await model.respond(**REQUEST)
    kw = rec.kwargs
    assert kw["store"] is False
    assert kw["include"] == ["reasoning.encrypted_content"]
    assert kw["safety_identifier"] == "u-123"
    assert kw["text"]["format"]["strict"] is True
    assert all(t["strict"] for t in kw["tools"])
    assert kw["input"] == [{"role": "user", "content": "where is my order?"}]


async def test_openai_tool_call_and_reasoning_are_kept_for_replay():
    reasoning = ResponseReasoningItem(id="rs_1", type="reasoning", summary=[],
                                      encrypted_content="enc")
    fc = ResponseFunctionToolCall(type="function_call", call_id="call_9", name="get_order",
                                  arguments='{"order_id": "1"}', id="fc_1", status="completed")
    model, _ = openai_model(openai_response([reasoning, fc]))

    turn = await model.respond(**REQUEST)

    assert turn.stop == "tool_calls"
    assert turn.tool_calls == [{"id": "call_9", "name": "get_order",
                                "arguments": {"order_id": "1"}}]
    assert turn.native[0]["encrypted_content"] == "enc"
    assert turn.usage["cache_read_input_tokens"] == 80


async def test_openai_refusal_and_truncation():
    refusal = ResponseOutputMessage.model_validate({
        "id": "m1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "refusal", "refusal": "no"}],
    })
    model, _ = openai_model(openai_response([refusal]))
    assert (await model.respond(**REQUEST)).stop == "refusal"

    model, _ = openai_model(openai_response([], status="incomplete"))
    assert (await model.respond(**REQUEST)).stop == "truncated"


def _req() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.example/v1")


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (openai.APIConnectionError(request=_req()), True),
        (openai.InternalServerError("x", response=httpx2.Response(500, request=_req()),
                                    body=None), True),
        (openai.AuthenticationError("x", response=httpx2.Response(401, request=_req()),
                                    body=None), False),
    ],
)
async def test_openai_errors_are_classified(exc, transient):
    model, _ = openai_model(exc)
    with pytest.raises(ModelUnavailable) as info:
        await model.respond(**REQUEST)
    assert info.value.transient is transient


def anthropic_model(result: Any, fallback: bool = True) -> tuple[ClaudeAgentModel, Recorder]:
    rec = Recorder(result)
    client = SimpleNamespace(beta=SimpleNamespace(messages=rec))
    return ClaudeAgentModel(client, "claude-test", "medium", fallback), rec  # type: ignore[arg-type]


def anthropic_response(content: list[Any], stop: str) -> Any:
    usage = SimpleNamespace(input_tokens=50, output_tokens=10, cache_read_input_tokens=None)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, model="claude-test")


async def test_claude_request_uses_refusal_fallback_and_cacheable_system():
    model, rec = anthropic_model(anthropic_response(
        [BetaTextBlock(type="text", text='{"answer":"hi"}')], "end_turn"))
    turn = await model.respond(**REQUEST)
    kw = rec.kwargs
    assert kw["betas"] == [ANTHROPIC_FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in kw and "thinking" not in kw
    assert turn.stop == "final" and turn.text == '{"answer":"hi"}'


async def test_claude_thinking_blocks_survive_for_replay():
    blocks = [
        BetaThinkingBlock(type="thinking", thinking="", signature="sig"),
        BetaToolUseBlock(type="tool_use", id="toolu_1", name="get_order",
                         input={"order_id": "1"}),
    ]
    model, _ = anthropic_model(anthropic_response(blocks, "tool_use"), fallback=False)
    turn = await model.respond(**REQUEST)
    assert turn.stop == "tool_calls"
    assert turn.native[0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    replayed = transcript.to_anthropic([transcript.user("q"), turn.as_message()])
    assert replayed[1]["content"][0]["signature"] == "sig"


async def test_claude_refusal_drops_partial_content():
    model, _ = anthropic_model(anthropic_response(
        [BetaTextBlock(type="text", text="partial")], "refusal"))
    turn = await model.respond(**REQUEST)
    assert turn.stop == "refusal" and turn.native == []


async def test_claude_errors_are_classified():
    err = anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=_req()),
                                   body=None)
    model, _ = anthropic_model(err)
    with pytest.raises(ModelUnavailable) as info:
        await model.respond(**REQUEST)
    assert info.value.transient
