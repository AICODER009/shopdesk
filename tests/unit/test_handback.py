from shopdesk.agent.handback import InMemoryOpenHandoffs, reconcile
from shopdesk.agent.handoff import InMemoryTicketSink
from shopdesk.integrations.zendesk.models import HandoffTicket


class RecordingTarget:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, int, str]] = []

    async def resolve_handoff(self, thread_id: str, ticket_id: int, status: str) -> bool:
        self.resolved.append((thread_id, ticket_id, status))
        return True


async def sink_with(n: int) -> InMemoryTicketSink:
    sink = InMemoryTicketSink()
    for i in range(n):
        await sink.create_ticket(HandoffTicket(conversation_id=f"t{i}", subject="s",
                                               internal_note="n", idempotency_key=f"k{i}"))
    return sink


async def test_solved_and_closed_tickets_are_handed_back_open_ones_wait():
    sink = await sink_with(3)
    sink.statuses = {1: "solved", 2: "open", 3: "closed"}
    store = InMemoryOpenHandoffs()
    for thread, ticket in (("a", 1), ("b", 2), ("c", 3)):
        await store.add(thread, ticket)
    target = RecordingTarget()

    stats = await reconcile(target, sink, store)

    assert stats == {"checked": 3, "handed_back": 2}
    assert target.resolved == [("a", 1, "solved"), ("c", 3, "closed")]
    assert await store.all() == [("b", 2)]


async def test_deleted_tickets_are_dropped_from_the_registry():
    sink = await sink_with(1)
    store = InMemoryOpenHandoffs()
    await store.add("gone", 99)
    target = RecordingTarget()

    await reconcile(target, sink, store)

    assert target.resolved == [] and await store.all() == []


async def test_nothing_open_means_no_zendesk_call():
    class Exploding(InMemoryTicketSink):
        async def ticket_statuses(self, ticket_ids):  # type: ignore[no-untyped-def]
            raise AssertionError("should not be called")

    assert await reconcile(RecordingTarget(), Exploding(), InMemoryOpenHandoffs()) == {
        "checked": 0, "handed_back": 0,
    }
