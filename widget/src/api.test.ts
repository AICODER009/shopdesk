import { ChatClient, ChatError } from "./api";

const reply = {
  conversation_id: "c-1",
  status: "answered",
  reply: "hi",
  citations: [],
  ticket_id: null,
};

function respond(...statuses: number[]) {
  const calls: RequestInit[] = [];
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    calls.push(init);
    const status = statuses.shift() ?? 200;
    return new Response(status === 200 ? JSON.stringify(reply) : "{}", { status });
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

test("sends the shopper token and conversation id", async () => {
  const { calls, fetchImpl } = respond(200);
  const client = new ChatClient("https://api.test", async () => "jwt-1", fetchImpl);

  const out = await client.send("hello", "c-0");

  expect(out.conversation_id).toBe("c-1");
  expect((calls[0].headers as Record<string, string>).Authorization).toBe("Bearer jwt-1");
  expect(JSON.parse(calls[0].body as string)).toEqual({ message: "hello", conversation_id: "c-0" });
});

test("guests send no authorization header", async () => {
  const { calls, fetchImpl } = respond(200);
  await new ChatClient("https://api.test", async () => null, fetchImpl).send("hi", null);
  expect((calls[0].headers as Record<string, string>).Authorization).toBeUndefined();
});

test("refreshes an expired token once", async () => {
  const { calls, fetchImpl } = respond(401, 200);
  const tokens = ["old", "new"];
  const client = new ChatClient("https://api.test", async () => tokens.shift()!, fetchImpl);

  await client.send("hi", null);

  expect(calls).toHaveLength(2);
  expect((calls[1].headers as Record<string, string>).Authorization).toBe("Bearer new");
});

test.each([
  [503, true],
  [422, false],
  [401, false],
])("HTTP %i maps to a ChatError (retryable=%s)", async (status, retryable) => {
  const { fetchImpl } = respond(status, status);
  const client = new ChatClient("https://api.test", async () => null, fetchImpl);
  const err = await client.send("hi", null).catch((e: unknown) => e);
  expect(err).toBeInstanceOf(ChatError);
  expect((err as ChatError).retryable).toBe(retryable);
});

test("network failures are retryable", async () => {
  const fetchImpl = (async () => {
    throw new TypeError("offline");
  }) as unknown as typeof fetch;
  const err = await new ChatClient("https://api.test", async () => null, fetchImpl)
    .send("hi", null)
    .catch((e: unknown) => e);
  expect((err as ChatError).retryable).toBe(true);
});
