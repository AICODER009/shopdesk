import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { ChatError, type ChatClient, type ChatReply } from "./api";

const options = { title: "Support", greeting: "Hi there", privacyNote: "note" };

function fakeClient(replies: Array<ChatReply | ChatError>) {
  const sent: Array<[string, string | null]> = [];
  const forgotten: string[] = [];
  const client = {
    send: async (message: string, id: string | null) => {
      sent.push([message, id]);
      const next = replies.shift()!;
      if (next instanceof ChatError) throw next;
      return next;
    },
    forget: async (id: string) => {
      forgotten.push(id);
    },
  } as unknown as ChatClient;
  return { client, sent, forgotten };
}

const answer = (over: Partial<ChatReply> = {}): ChatReply => ({
  conversation_id: "conv-1",
  status: "answered",
  reply: "You have 30 days.",
  citations: [{ title: "Returns policy", url: "https://help.example.com/returns" }],
  ticket_id: null,
  ...over,
});

beforeEach(() => sessionStorage.clear());

async function openAndSend(text: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Open chat" }));
  await user.type(screen.getByLabelText("Message"), `${text}{Enter}`);
  return user;
}

test("shows the answer with its sources and keeps the conversation id", async () => {
  const { client, sent } = fakeClient([answer(), answer({ reply: "Sure." })]);
  render(<App client={client} options={options} />);

  const user = await openAndSend("return window?");
  expect(await screen.findByText("You have 30 days.")).toBeInTheDocument();
  const link = screen.getByRole("link", { name: "Returns policy" });
  expect(link).toHaveAttribute("rel", "noopener noreferrer");

  await user.type(screen.getByLabelText("Message"), "thanks{Enter}");
  await screen.findByText("Sure.");
  expect(sent).toEqual([
    ["return window?", null],
    ["thanks", "conv-1"],
  ]);
});

test("handoff shows the ticket chip", async () => {
  const { client } = fakeClient([
    answer({ status: "handed_off", reply: "Passed to our team.", citations: [], ticket_id: 42 }),
  ]);
  render(<App client={client} options={options} />);
  await openAndSend("refund please");
  expect(await screen.findByText("With our support team")).toBeInTheDocument();
  expect(screen.getByText("Ticket #42")).toBeInTheDocument();
});

test("non-http citation urls are not rendered as links", async () => {
  const { client } = fakeClient([
    answer({ citations: [{ title: "Sneaky", url: "javascript:alert(1)" }] }),
  ]);
  render(<App client={client} options={options} />);
  await openAndSend("hi");
  expect(await screen.findByText("Sneaky")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Sneaky" })).toBeNull();
});

test("retryable errors offer a retry that resends the message", async () => {
  const { client, sent } = fakeClient([new ChatError("Can't reach support.", true), answer()]);
  render(<App client={client} options={options} />);
  const user = await openAndSend("where is my parcel");

  await user.click(await screen.findByRole("button", { name: "Try again" }));

  await screen.findByText("You have 30 days.");
  expect(sent.map(([m]) => m)).toEqual(["where is my parcel", "where is my parcel"]);
});

test("delete asks first, then erases on the server and locally", async () => {
  const { client, forgotten } = fakeClient([answer()]);
  render(<App client={client} options={options} />);
  const user = await openAndSend("hello");
  await screen.findByText("You have 30 days.");

  await user.click(screen.getByRole("button", { name: "Delete conversation" }));
  await user.click(screen.getByRole("button", { name: "Delete" }));

  await waitFor(() => expect(screen.queryByText("You have 30 days.")).toBeNull());
  expect(forgotten).toEqual(["conv-1"]);
  expect(sessionStorage.getItem("shopdesk.conversation")).toContain('"messages":[]');
});

test("conversation survives a remount within the tab", async () => {
  const { client } = fakeClient([answer()]);
  const { unmount } = render(<App client={client} options={options} />);
  await openAndSend("hello");
  await screen.findByText("You have 30 days.");
  unmount();

  render(<App client={client} options={options} />);
  await userEvent.setup().click(screen.getByRole("button", { name: "Open chat" }));
  expect(screen.getByText("You have 30 days.")).toBeInTheDocument();
});

test("suggestion chips send their question and disappear once the chat starts", async () => {
  const { client, sent } = fakeClient([answer()]);
  render(<App client={client} options={options} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Open chat" }));

  await user.click(screen.getByRole("button", { name: "Start a return" }));

  await screen.findByText("You have 30 days.");
  expect(sent[0][0]).toBe("How do I return something?");
  expect(screen.queryByRole("button", { name: "Start a return" })).toBeNull();
});

test("a reply that arrives while the panel is closed shows an unread badge", async () => {
  let release: (r: ChatReply) => void = () => {};
  const client = {
    send: () => new Promise<ChatReply>((resolve) => (release = resolve)),
    forget: async () => {},
  } as unknown as ChatClient;
  render(<App client={client} options={options} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Open chat" }));
  await user.type(screen.getByLabelText("Message"), "hello{Enter}");
  await user.click(screen.getAllByRole("button", { name: "Close chat" })[0]);

  release(answer());

  expect(await screen.findByRole("button", { name: "Open chat, new reply" })).toBeInTheDocument();
});

test("bare URLs in replies become safe links; other text stays text", async () => {
  const { client } = fakeClient([
    answer({ reply: "Track it here: https://www.dhl.com/track?id=JD01. Or javascript:alert(1)", citations: [] }),
  ]);
  render(<App client={client} options={options} />);
  await openAndSend("where is it");
  const link = await screen.findByRole("link", { name: "https://www.dhl.com/track?id=JD01" });
  expect(link).toHaveAttribute("href", "https://www.dhl.com/track?id=JD01");
  expect(screen.queryByRole("link", { name: /javascript/ })).toBeNull();
});
