"""The support agent as a LangGraph state machine.

    guard ──► agent ◄──────────────┐
      │        │  ├─► tools ───────┤ (more tool calls)
      ▼        │  │     └─► handoff ──► await_human (interrupt until Zendesk says solved)
   blocked     │  └─► finalize ────┘ (one repair round if the reply fails checks)
               │
     retries ──┴─► error_handler: auto-handoff (signed in) / apology (guest)

Failure policy, in order:
1. transient provider/upstream errors retry with backoff (RetryPolicy, per node);
2. the last LLM attempt goes to the fallback provider;
3. exhausted LLM retries hand the customer to a human rather than dropping them;
4. exhausted handoff retries park the ticket in the dead-letter queue for replay.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, Command, RetryPolicy, TimeoutPolicy, interrupt
from pydantic import ValidationError

from shopdesk.agent import transcript
from shopdesk.agent.dead_letters import DeadLetterStore
from shopdesk.agent.handoff import TicketSink, build_ticket
from shopdesk.agent.llm import AgentModel, ModelTurn, ModelUnavailable
from shopdesk.agent.prompts import HANDBACK_NOTE, SUSPICIOUS_NOTE, build_instructions
from shopdesk.agent.reply import REPLY_SCHEMA, check_reply, citations_for, parse_reply
from shopdesk.agent.state import AgentState, CustomerContext, TurnOutcome
from shopdesk.agent.tools import HANDOFF_TOOL, TOOL_SPECS, HandoffArgs, Toolbox, render_search
from shopdesk.config import Settings
from shopdesk.integrations.http import RETRYABLE_STATUS, UpstreamError
from shopdesk.integrations.zendesk.models import HandoffTicket, TicketRef
from shopdesk.privacy import redact_for_model
from shopdesk.security.injection import InjectionGuard

log = structlog.get_logger()

BLOCKED_REPLY = (
    "I can help with orders, deliveries, returns, payments and your account. "
    "What can I help you with?"
)
DECLINE_REPLY = (
    "I'm not able to help with that one here. If it's about an order or your account, "
    "I can connect you with someone from our team."
)
UNSURE_REPLY = (
    "I don't want to give you wrong information on this. Would you like me to connect you "
    "with someone from our team?"
)
EXHAUSTED_REPLY = (
    "I haven't been able to sort this out myself. Would you like me to pass it to our team? "
    "They can pick it up with everything we've covered so far."
)
HANDOFF_REPLY = (
    "I've passed this to our support team (ticket #{ticket_id}). They'll reply {channel}, "
    "usually within one business day."
)
QUEUED_REPLY = (
    "I've passed this to our support team. They'll reply {channel}, usually within one "
    "business day."
)
OUTAGE_HANDOFF_SUMMARY = (
    "The AI assistant couldn't respond (model outage). Please pick up from the customer's "
    "last message."
)


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, ModelUnavailable):
        return exc.transient
    if isinstance(exc, UpstreamError):
        return exc.status is None or exc.status in RETRYABLE_STATUS
    return isinstance(exc, NodeTimeoutError | httpx.TransportError | TimeoutError)


def reply_message(text: str) -> dict[str, Any]:
    # Deterministic replies are logged in the same JSON shape the model writes, so the
    # model sees one consistent format when it reads the history back.
    body = {"answer": text, "citations": [], "grounded_in": "none", "confidence": "high"}
    return transcript.assistant(text=json.dumps(body))


def outcome(status: str, reply: str, **extra: Any) -> dict[str, Any]:
    return TurnOutcome(status=status, reply=reply, **extra).model_dump(mode="json")


@dataclass
class AgentDeps:
    settings: Settings
    primary: AgentModel
    fallback: AgentModel | None
    guard: InjectionGuard
    toolbox: Toolbox
    tickets: TicketSink
    dead_letters: DeadLetterStore
    canary: str


class SupportAgent:
    def __init__(self, deps: AgentDeps) -> None:
        self.d = deps
        self.s = deps.settings

    def compile(self, checkpointer: Checkpointer) -> CompiledStateGraph:  # type: ignore[type-arg]
        s = self.s
        g = StateGraph(AgentState)
        g.add_node("guard", self.guard, destinations=("agent", "blocked"),
                   retry_policy=RetryPolicy(max_attempts=2, retry_on=is_transient))
        g.add_node(
            "agent",
            self.agent,
            retry_policy=RetryPolicy(
                max_attempts=s.llm_attempts,
                initial_interval=s.retry_initial_interval,
                max_interval=8.0,
                retry_on=is_transient,
            ),
            timeout=TimeoutPolicy(run_timeout=s.llm_timeout_seconds),
            error_handler=self.agent_failed,
            destinations=("tools", "finalize", "handoff", END),
        )
        g.add_node("tools", self.tools, destinations=("agent", "handoff", END))
        g.add_node("finalize", self.finalize, destinations=("agent", END))
        g.add_node(
            "handoff",
            self.handoff,
            # Safe to retry: every attempt sends the same Idempotency-Key.
            retry_policy=RetryPolicy(
                max_attempts=s.handoff_attempts,
                initial_interval=s.retry_initial_interval,
                max_interval=10.0,
                retry_on=is_transient,
            ),
            timeout=30,
            error_handler=self.handoff_failed,
            destinations=("await_human",),
        )
        g.add_node("await_human", self.await_human)
        g.add_node("blocked", self.blocked)
        g.add_edge(START, "guard")
        g.add_edge("await_human", END)
        g.add_edge("blocked", END)
        return g.compile(checkpointer=checkpointer)

    # -- nodes -------------------------------------------------------------------------

    async def guard(self, state: AgentState) -> Command:  # type: ignore[type-arg]
        text = redact_for_model(state["incoming"]).strip()
        result = await self.d.guard.check(text)
        update: dict[str, Any] = {
            "incoming": "",
            "guard": {"label": result.label, "heuristics": result.heuristics},
        }
        if result.blocked:
            log.warning("guard.blocked", heuristics=result.heuristics, reason=result.reason)
            return Command(goto="blocked", update=update)

        messages = [transcript.user(text)]
        if result.label == "suspicious":
            messages.append(transcript.note(SUSPICIOUS_NOTE))
        return Command(goto="agent", update=update | {"messages": messages})

    async def blocked(self, state: AgentState) -> dict[str, Any]:
        # The blocked text is not written to the transcript: nothing for the model to
        # re-read later, and nothing extra retained about the customer.
        return {"outcome": outcome("blocked", BLOCKED_REPLY)}

    def _model_for(self, attempt: int) -> AgentModel:
        if self.d.fallback is not None and attempt >= self.s.llm_attempts:
            return self.d.fallback
        return self.d.primary

    async def agent(self, state: AgentState, runtime: Runtime) -> Command:  # type: ignore[type-arg]
        customer = CustomerContext.model_validate(state["customer"])
        info = runtime.execution_info
        model = self._model_for(info.node_attempt if info else 1)
        instructions = build_instructions(self.s.brand_name, self.d.canary, customer)

        async def respond(m: AgentModel) -> ModelTurn:
            return await m.respond(instructions=instructions, messages=state["messages"],
                                   tools=TOOL_SPECS, output_schema=REPLY_SCHEMA,
                                   user_ref=state.get("user_ref"))

        try:
            turn = await respond(model)
        except ModelUnavailable as exc:
            # Bad key, unknown model, permission errors: retrying the same provider won't
            # help, so go straight to the fallback instead of burning attempts.
            if exc.transient or model is not self.d.primary or self.d.fallback is None:
                raise
            log.error("llm.primary_unusable", error=str(exc))
            turn = await respond(self.d.fallback)

        steps = state.get("turn_steps", 0) + 1
        if turn.stop in ("refusal", "truncated"):
            log.warning("llm.no_answer", stop=turn.stop, provider=turn.provider)
            return Command(
                goto=END,
                update={
                    "turn_steps": steps,
                    "messages": [reply_message(DECLINE_REPLY)],
                    "outcome": outcome("declined", DECLINE_REPLY),
                },
            )
        return Command(
            goto="tools" if turn.stop == "tool_calls" else "finalize",
            update={"turn_steps": steps, "messages": [turn.as_message()]},
        )

    async def agent_failed(self, state: AgentState, error: NodeError) -> Command:  # type: ignore[type-arg]
        log.error("agent.exhausted", error=repr(error.error))
        customer = CustomerContext.model_validate(state["customer"])
        if customer.signed_in:
            ticket = build_ticket(
                conversation_id=state["thread_id"],
                tool_use_id=f"outage-{len(state['messages'])}",
                customer=customer,
                reason="assistant_unavailable",
                summary=OUTAGE_HANDOFF_SUMMARY,
                priority="normal",
                contact_email="",
                messages=state["messages"],
                order_ids=state.get("order_ids", []),
            )
            pending: dict[str, Any] = {"ticket": ticket.model_dump(mode="json"), "call_id": None,
                                       "results": []}
            return Command(goto="handoff", update={"pending_handoff": pending})

        reply = (
            "Sorry, I'm having trouble answering right now. Please try again in a few minutes, "
            f"or email us at {self.s.support_email}."
        )
        return Command(
            goto=END,
            update={"messages": [reply_message(reply)], "outcome": outcome("error", reply)},
        )

    async def tools(self, state: AgentState) -> Command:  # type: ignore[type-arg]
        customer = CustomerContext.model_validate(state["customer"])
        calls = state["messages"][-1]["tool_calls"]
        handoffs = [c for c in calls if c["name"] == HANDOFF_TOOL]
        runs = await asyncio.gather(
            *(self.d.toolbox.run(c, customer) for c in calls if c["name"] != HANDOFF_TOOL)
        )

        next_id = len(state.get("sources") or {}) + 1
        sources: dict[str, dict[str, Any]] = {}
        order_ids: list[str] = []
        results: list[transcript.ToolResult] = []
        for run in runs:
            if run.retrieval is not None:
                run.content, found = render_search(run, next_id)
                next_id += len(found)
                sources |= found
            order_ids += run.order_ids
            results.append(run.result())
        update: dict[str, Any] = {"sources": sources, "order_ids": order_ids}

        if handoffs:
            first, *extra = handoffs
            results += [
                {"call_id": c["id"], "name": HANDOFF_TOOL, "is_error": True,
                 "content": "Handoff already requested in this turn."}
                for c in extra
            ]
            routed = self._prepare_handoff(state, customer, first, results, order_ids)
            if routed is not None:
                return Command(goto="handoff", update=update | {"pending_handoff": routed})

        update["messages"] = [transcript.tool_results(results)]
        if state.get("turn_steps", 0) >= self.s.max_agent_steps:
            log.warning("agent.step_budget_exhausted", steps=state.get("turn_steps"))
            update["messages"].append(reply_message(EXHAUSTED_REPLY))
            update["outcome"] = outcome("declined", EXHAUSTED_REPLY)
            return Command(goto=END, update=update)
        return Command(goto="agent", update=update)

    def _prepare_handoff(
        self,
        state: AgentState,
        customer: CustomerContext,
        call: transcript.ToolCall,
        results: list[transcript.ToolResult],
        order_ids: list[str],
    ) -> dict[str, Any] | None:
        """Returns the pending handoff, or None after appending an error result for the model."""
        try:
            args = HandoffArgs.model_validate(call["arguments"])
        except ValidationError as exc:
            results.append({"call_id": call["id"], "name": HANDOFF_TOOL, "is_error": True,
                            "content": f"Invalid arguments: {exc.errors()[0]['msg']}"})
            return None

        ticket = build_ticket(
            conversation_id=state["thread_id"],
            tool_use_id=call["id"],
            customer=customer,
            reason=args.reason,
            summary=args.summary,
            priority=args.priority,
            contact_email=args.contact_email,
            messages=state["messages"],
            order_ids=[*state.get("order_ids", []), *order_ids],
        )
        if ticket.requester_email is None:
            results.append({
                "call_id": call["id"], "name": HANDOFF_TOOL, "is_error": True,
                "content": "The team needs a way to reply to a guest. Ask the customer for the "
                           "email address they'd like the reply sent to (or to sign in), then "
                           "call handoff_to_human again with contact_email set.",
            })
            return None
        return {"ticket": ticket.model_dump(mode="json"), "call_id": call["id"],
                "results": results}

    async def handoff(self, state: AgentState) -> Command:  # type: ignore[type-arg]
        pending = state["pending_handoff"]
        assert pending is not None
        ref = await self.d.tickets.create_ticket(HandoffTicket.model_validate(pending["ticket"]))
        log.info("handoff.created", ticket_id=ref.id)
        return self._handed_off(pending, ref)

    async def handoff_failed(self, state: AgentState, error: NodeError) -> Command:  # type: ignore[type-arg]
        pending = state["pending_handoff"]
        assert pending is not None
        ticket = HandoffTicket.model_validate(pending["ticket"])
        letter_id = await self.d.dead_letters.add(state["thread_id"], ticket, repr(error.error))
        log.error("handoff.dead_lettered", letter_id=letter_id, error=repr(error.error))
        return self._handed_off(pending, None)

    def _handed_off(self, pending: dict[str, Any], ref: TicketRef | None) -> Command:  # type: ignore[type-arg]
        ticket = HandoffTicket.model_validate(pending["ticket"])
        channel = "by email" if ticket.requester_email else "here"
        reply = (HANDOFF_REPLY.format(ticket_id=ref.id, channel=channel) if ref
                 else QUEUED_REPLY.format(channel=channel))

        messages: list[dict[str, Any]] = []
        if pending["call_id"]:
            status = f"Ticket #{ref.id} created." if ref else "Queued for the support team."
            results = [*pending["results"], {
                "call_id": pending["call_id"], "name": HANDOFF_TOOL, "is_error": False,
                "content": f"{status} A human agent now owns this conversation.",
            }]
            messages.append(transcript.tool_results(results))
        messages.append(reply_message(reply))
        return Command(
            goto="await_human",
            update={
                "messages": messages,
                "pending_handoff": None,
                "takeover": {
                    "ticket": ref.model_dump() if ref else None,
                    "queued": ref is None,
                    "since": datetime.now(UTC).isoformat(),
                },
                "outcome": outcome("handed_off", reply, ticket=ref),
            },
        )

    async def await_human(self, state: AgentState) -> dict[str, Any]:
        takeover = state.get("takeover") or {}
        # Paused here until ChatService.resolve_handoff resumes us (Zendesk webhook on solve,
        # or takeover expiry). Messages that arrive meanwhile go to the ticket, not the model.
        decision = interrupt({"ticket": takeover.get("ticket"), "queued": takeover.get("queued")})
        note = HANDBACK_NOTE.format(ticket_id=decision.get("ticket_id") or "?",
                                    status=decision.get("status", "solved"))
        return {"messages": [transcript.note(note)], "takeover": None}

    async def finalize(self, state: AgentState) -> Command:  # type: ignore[type-arg]
        sources = state.get("sources") or {}
        draft = parse_reply(state["messages"][-1]["text"])
        problems = (["The reply wasn't valid JSON for the reply schema."] if draft is None
                    else check_reply(draft, sources, self.d.canary))

        if draft is not None and not problems:
            return Command(
                goto=END,
                update={"outcome": outcome("answered", draft.answer.strip(),
                                           citations=citations_for(draft, sources))},
            )

        log.warning("reply.rejected", problems=problems, repairs=state.get("repairs", 0))
        if state.get("repairs", 0) < 1 and state.get("turn_steps", 0) < self.s.max_agent_steps:
            feedback = "Your last reply failed checks: " + " ".join(problems) + \
                       " Send a corrected reply."
            return Command(goto="agent",
                           update={"repairs": 1, "messages": [transcript.note(feedback)]})
        return Command(
            goto=END,
            update={"messages": [reply_message(UNSURE_REPLY)],
                    "outcome": outcome("declined", UNSURE_REPLY)},
        )
