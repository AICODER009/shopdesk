import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChatClient, ChatError } from "./api";
import { Icon, type IconName } from "./icons";
import * as store from "./store";
import type { Conversation, Message } from "./store";

export interface Suggestion {
  label: string;
  message: string;
  icon: IconName;
}

export interface WidgetOptions {
  title: string;
  greeting: string;
  privacyNote: string;
  suggestions?: Suggestion[];
}

const MAX_LENGTH = 4000;
const COUNTER_FROM = 3500;

export const DEFAULT_SUGGESTIONS: Suggestion[] = [
  { label: "Track my order", message: "Where is my order?", icon: "truck" },
  { label: "Start a return", message: "How do I return something?", icon: "undo" },
  { label: "Delivery times", message: "How long does delivery take?", icon: "package" },
  { label: "Talk to a person", message: "I'd like to talk to a person.", icon: "headset" },
];

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

export function App({ client, options }: { client: ChatClient; options: WidgetOptions }) {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(false);
  const [conversation, setConversation] = useState<Conversation>(store.load);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => store.save(conversation), [conversation]);

  useEffect(() => {
    if (open) {
      setUnread(false);
      inputRef.current?.focus();
    }
  }, [open]);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [conversation.messages, sending, open]);

  // Grow the composer with its content, up to the CSS max-height.
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }, [draft, open]);

  const close = () => {
    setOpen(false);
    launcherRef.current?.focus();
  };

  const append = (message: Message, id?: string | null) =>
    setConversation((c) => ({
      id: id === undefined ? c.id : id,
      messages: [...c.messages, message],
    }));

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || sending) return;
      setFailed(null);
      setDraft("");
      append({ id: store.messageId(), role: "customer", text: message, at: now() });
      setSending(true);
      try {
        const reply = await client.send(message, conversation.id);
        append(
          {
            id: store.messageId(),
            role: "assistant",
            text: reply.reply,
            status: reply.status,
            citations: reply.citations,
            ticketId: reply.ticket_id,
            at: now(),
          },
          reply.conversation_id,
        );
        if (!openRef.current) setUnread(true);
      } catch (err) {
        const error = err instanceof ChatError ? err : new ChatError("Something went wrong.", true);
        append({ id: store.messageId(), role: "system", text: error.message, at: now() });
        if (error.retryable) setFailed(message);
      } finally {
        setSending(false);
      }
    },
    [client, conversation.id, sending],
  );

  const deleteConversation = async () => {
    setConfirmingDelete(false);
    if (conversation.id) {
      try {
        await client.forget(conversation.id);
      } catch {
        append({
          id: store.messageId(),
          role: "system",
          text: "Couldn't delete the conversation. Please try again.",
          at: now(),
        });
        return;
      }
    }
    store.clear();
    setConversation({ id: null, messages: [] });
    setFailed(null);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send(draft);
    }
  };

  const empty = conversation.messages.length === 0;
  const suggestions = options.suggestions ?? DEFAULT_SUGGESTIONS;

  return (
    <div className="sd-root" onKeyDown={(e) => e.key === "Escape" && open && close()}>
      {open && (
        <section className="sd-panel" role="dialog" aria-label={options.title}>
          <header className="sd-header">
            <div className="sd-identity">
              <span className="sd-avatar" aria-hidden="true">
                <Icon name="sparkles" size={18} />
              </span>
              <div>
                <h2>{options.title}</h2>
                <p className="sd-presence">
                  <span className="sd-dot" aria-hidden="true" />
                  AI assistant · our team is one message away
                </p>
              </div>
            </div>
            <div className="sd-actions">
              {!empty && (
                <button
                  type="button"
                  className="sd-icon-btn"
                  onClick={() => setConfirmingDelete(true)}
                  aria-label="Delete conversation"
                  title="Delete conversation"
                >
                  <Icon name="trash" size={17} />
                </button>
              )}
              <button
                type="button"
                className="sd-icon-btn"
                onClick={close}
                aria-label="Close chat"
                title="Close"
              >
                <Icon name="chevron-down" size={20} />
              </button>
            </div>
          </header>

          {confirmingDelete && (
            <div className="sd-confirm" role="alertdialog" aria-label="Delete conversation">
              <p>
                <strong>Delete this conversation?</strong>
                <span>It's removed from our systems too. This can't be undone.</span>
              </p>
              <div>
                <button
                  type="button"
                  className="sd-btn sd-btn-ghost"
                  onClick={() => setConfirmingDelete(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="sd-btn sd-btn-danger"
                  onClick={() => void deleteConversation()}
                >
                  Delete
                </button>
              </div>
            </div>
          )}

          <div className="sd-log" ref={logRef} aria-live="polite" aria-busy={sending}>
            <div className="sd-welcome">
              <p className="sd-eyebrow">{options.title}</p>
              <p className="sd-greeting">{options.greeting}</p>
            </div>

            {empty && (
              <div className="sd-suggestions" aria-label="Suggested questions">
                {suggestions.map((s) => (
                  <button
                    key={s.label}
                    type="button"
                    className="sd-suggestion"
                    onClick={() => void send(s.message)}
                  >
                    <Icon name={s.icon} size={16} />
                    {s.label}
                  </button>
                ))}
              </div>
            )}

            {conversation.messages.map((m, i) => (
              <MessageView
                key={m.id}
                message={m}
                grouped={i > 0 && conversation.messages[i - 1].role === m.role}
              />
            ))}

            {sending && (
              <div className="sd-row sd-row-assistant">
                <div className="sd-bubble sd-bubble-assistant sd-typing" aria-label="Assistant is typing">
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            )}

            {failed && !sending && (
              <button type="button" className="sd-retry" onClick={() => void send(failed)}>
                <Icon name="rotate-ccw" size={14} />
                Try again
              </button>
            )}
          </div>

          <form
            className="sd-composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send(draft);
            }}
          >
            <div className="sd-field">
              <textarea
                ref={inputRef}
                value={draft}
                maxLength={MAX_LENGTH}
                rows={1}
                placeholder="Ask about an order, delivery or return…"
                aria-label="Message"
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={onKeyDown}
              />
              {draft.length >= COUNTER_FROM && (
                <span className="sd-counter" aria-live="polite">
                  {MAX_LENGTH - draft.length}
                </span>
              )}
            </div>
            <button
              type="submit"
              className="sd-send"
              disabled={!draft.trim() || sending}
              aria-label="Send"
            >
              <Icon name="send" size={18} />
            </button>
          </form>
          <p className="sd-note">
            <Icon name="shield-check" size={13} />
            {options.privacyNote}
          </p>
        </section>
      )}

      <button
        ref={launcherRef}
        type="button"
        className="sd-launcher"
        data-open={open}
        onClick={() => (open ? close() : setOpen(true))}
        aria-label={open ? "Close chat" : unread ? "Open chat, new reply" : "Open chat"}
        aria-expanded={open}
      >
        <span className="sd-launcher-icon sd-launcher-chat">
          <Icon name="message-circle" size={24} />
        </span>
        <span className="sd-launcher-icon sd-launcher-close">
          <Icon name="x" size={24} />
        </span>
        {unread && !open && <span className="sd-badge" aria-hidden="true" />}
      </button>
    </div>
  );
}

const URL_PATTERN = /(https?:\/\/[^\s<>"')\]]+[^\s<>"')\].,;:!?])/g;

// Turns bare http(s) URLs in a reply into links. Rendered as React elements, never as HTML.
function Linkified({ text }: { text: string }) {
  const parts = text.split(URL_PATTERN);
  return (
    <>
      {parts.map((part, i) => {
        const url = i % 2 === 1 ? store.safeUrl(part) : null;
        return url ? (
          <a key={i} href={url} target="_blank" rel="noopener noreferrer">
            {part}
          </a>
        ) : (
          part
        );
      })}
    </>
  );
}

function now(): string {
  return new Date().toISOString();
}

function MessageView({ message, grouped }: { message: Message; grouped: boolean }) {
  if (message.role === "system") {
    return (
      <div className="sd-notice" role="status">
        <Icon name="circle-alert" size={14} />
        <span>{message.text}</span>
      </div>
    );
  }

  const links = (message.citations ?? [])
    .map((c) => ({ title: c.title, url: store.safeUrl(c.url) }))
    .filter((c) => c.title);
  const time = message.at ? timeFormat.format(new Date(message.at)) : null;
  const role = message.role === "customer" ? "customer" : "assistant";

  return (
    <div className={`sd-row sd-row-${role}${grouped ? " sd-grouped" : ""}`}>
      <div className={`sd-bubble sd-bubble-${role}`}>
        <p>
          <Linkified text={message.text} />
        </p>
        {links.length > 0 && (
          <div className="sd-sources">
            <span className="sd-sources-label">Sources</span>
            {links.map((c) =>
              c.url ? (
                <a key={c.title} href={c.url} target="_blank" rel="noopener noreferrer">
                  <Icon name="book-open" size={14} />
                  <span>{c.title}</span>
                  <Icon name="external-link" size={12} />
                </a>
              ) : (
                <span key={c.title} className="sd-source-plain">
                  <Icon name="book-open" size={14} />
                  <span>{c.title}</span>
                </span>
              ),
            )}
          </div>
        )}
      </div>
      {message.status === "handed_off" && (
        <div className="sd-handoff">
          <span className="sd-handoff-icon" aria-hidden="true">
            <Icon name="headset" size={16} />
          </span>
          <div>
            <strong>With our support team</strong>
            <span>{message.ticketId ? `Ticket #${message.ticketId}` : "Request received"}</span>
          </div>
        </div>
      )}
      {time && <time className="sd-time" dateTime={message.at}>{time}</time>}
    </div>
  );
}
