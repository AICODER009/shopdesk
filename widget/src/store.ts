import type { Citation, Status } from "./api";

export interface Message {
  id: string;
  role: "customer" | "assistant" | "system";
  text: string;
  status?: Status;
  citations?: Citation[];
  ticketId?: number | null;
  at?: string;
}

export interface Conversation {
  id: string | null;
  messages: Message[];
}

// sessionStorage, not localStorage: the transcript is personal data and shouldn't outlive
// the tab. The server keeps the authoritative copy (and deletes it after the TTL).
const KEY = "shopdesk.conversation";

export function load(): Conversation {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (raw) return JSON.parse(raw) as Conversation;
  } catch {
    // storage blocked or corrupt; start fresh
  }
  return { id: null, messages: [] };
}

export function save(conversation: Conversation): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(conversation));
  } catch {
    // private mode / quota: the chat still works, it just won't survive a reload
  }
}

export function clear(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

let counter = 0;
export function messageId(): string {
  counter += 1;
  return `${Date.now().toString(36)}-${counter}`;
}

export function safeUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}
