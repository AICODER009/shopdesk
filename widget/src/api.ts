export type Status = "answered" | "declined" | "handed_off" | "blocked" | "error";

export interface Citation {
  title: string;
  url: string | null;
}

export interface ChatReply {
  conversation_id: string;
  status: Status;
  reply: string;
  citations: Citation[];
  ticket_id: number | null;
}

export class ChatError extends Error {
  constructor(
    message: string,
    readonly retryable: boolean,
  ) {
    super(message);
  }
}

export type TokenSource = () => Promise<string | null>;

const TIMEOUT_MS = 60_000;

export class ChatClient {
  private token: string | null | undefined;

  constructor(
    private readonly apiBase: string,
    private readonly getToken: TokenSource,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  async send(message: string, conversationId: string | null): Promise<ChatReply> {
    const body = JSON.stringify({ message, conversation_id: conversationId });
    const resp = await this.request("POST", "/v1/chat", body);
    return (await resp.json()) as ChatReply;
  }

  async forget(conversationId: string): Promise<void> {
    await this.request("DELETE", `/v1/conversations/${encodeURIComponent(conversationId)}`);
  }

  private async request(method: string, path: string, body?: string): Promise<Response> {
    let resp = await this.attempt(method, path, body);
    if (resp.status === 401) {
      // Shopper tokens are short-lived; fetch a fresh one once before giving up.
      this.token = undefined;
      resp = await this.attempt(method, path, body);
    }
    if (resp.ok) return resp;
    if (resp.status === 422) throw new ChatError("That message is too long.", false);
    if (resp.status === 401) throw new ChatError("Please sign in again to continue.", false);
    throw new ChatError("Something went wrong on our side. Please try again.", true);
  }

  private async attempt(method: string, path: string, body?: string): Promise<Response> {
    if (this.token === undefined) this.token = await this.getToken();
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      return await this.fetchImpl(`${this.apiBase}${path}`, {
        method,
        headers,
        body,
        signal: controller.signal,
      });
    } catch {
      throw new ChatError("Can't reach support right now. Check your connection.", true);
    } finally {
      clearTimeout(timer);
    }
  }
}

export function tokenFromUrl(url: string | undefined): TokenSource {
  if (!url) return async () => null;
  return async () => {
    try {
      const resp = await fetch(url, { credentials: "include" });
      if (!resp.ok) return null;
      const data = (await resp.json()) as { token?: string | null };
      return data.token ?? null;
    } catch {
      return null;
    }
  };
}
