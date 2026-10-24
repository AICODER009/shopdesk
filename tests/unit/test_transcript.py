from shopdesk.agent import transcript as t

THINKING = {"type": "thinking", "thinking": "", "signature": "sig=="}
REASONING = {"type": "reasoning", "id": "rs_1", "encrypted_content": "blob", "summary": []}

LOG = [
    t.user("where is 100245?"),
    t.note("flagged by screen"),
    t.assistant(
        tool_calls=[{"id": "toolu_1", "name": "get_order", "arguments": {"order_id": "100245"}}],
        provider="anthropic",
        native=[THINKING, {"type": "tool_use", "id": "toolu_1", "name": "get_order",
                           "input": {"order_id": "100245"}}],
    ),
    t.tool_results([{"call_id": "toolu_1", "name": "get_order", "content": "{}",
                     "is_error": False}]),
    t.assistant(text='{"answer":"On its way"}', provider="openai",
                native=[REASONING, {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text",
                                                 "text": '{"answer":"On its way"}'}]}]),
]


def test_anthropic_replays_its_own_thinking_and_merges_user_turns():
    out = t.to_anthropic(LOG)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert out[0]["content"][1]["text"].startswith("[Platform note")
    assert out[1]["content"][0] == THINKING
    assert out[2]["content"][0]["type"] == "tool_result"
    # OpenAI's reasoning blob must not leak into an Anthropic request.
    assert out[3]["content"] == [{"type": "text", "text": '{"answer":"On its way"}'}]


def test_openai_replays_its_own_reasoning_and_converts_the_rest():
    out = t.to_openai(LOG)
    assert out[0] == {"role": "user", "content": "where is 100245?"}
    assert out[1] == {"role": "developer", "content": "flagged by screen"}
    assert out[2] == {"type": "function_call", "call_id": "toolu_1", "name": "get_order",
                      "arguments": '{"order_id": "100245"}'}
    assert out[3]["type"] == "function_call_output"
    assert out[4] == REASONING
    assert THINKING not in out


def test_rendering_is_stable_for_prompt_caching():
    assert t.to_anthropic(LOG) == t.to_anthropic(LOG)
    assert t.to_openai(LOG[:3]) == t.to_openai(LOG)[:3]
