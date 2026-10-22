"use client";

import { useEffect, useRef, useState } from "react";
import MessageBubble from "./MessageBubble";
import { ApiError, sendMessage } from "@/lib/api";
import { suggestionsFor } from "@/lib/suggestions";
import type { ChatMessage } from "@/lib/types";

type Props = {
  sessionId: string;
  customerId?: string;
  firstName?: string;
  showDetails: boolean;
  onTurnComplete: () => void;
  onSessionExpired: () => void;
};

let nextId = 0;
const newId = () => `m${++nextId}`;

export default function Chat({ sessionId, customerId, firstName, showDetails, onTurnComplete, onSessionExpired }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  function patch(id: string, update: (m: ChatMessage) => ChatMessage) {
    setMessages((all) => all.map((m) => (m.id === id ? update(m) : m)));
  }

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setInput("");
    const reply = newId();
    setMessages((all) => [
      ...all,
      { id: newId(), role: "user", text: trimmed, activities: [], sources: [], streaming: false },
      { id: reply, role: "assistant", text: "", activities: [], sources: [], streaming: true },
    ]);

    try {
      await sendMessage(sessionId, trimmed, {
        onTool: ({ name, label }) =>
          patch(reply, (m) => ({ ...m, activities: [...m.activities, { name, label, status: "running" }] })),
        onToolResult: ({ name, ok, error, system }) =>
          patch(reply, (m) => {
            const idx = m.activities.findLastIndex((a) => a.name === name && a.status === "running");
            if (idx < 0) return m;
            const activities = [...m.activities];
            // A refusal like NOT_ELIGIBLE is a valid answer, not a failure of the lookup.
            const failed = !ok && error !== "NOT_ELIGIBLE" && error !== "NOT_FOUND";
            activities[idx] = { ...activities[idx], status: failed ? "error" : "ok", error, system };
            return { ...m, activities };
          }),
        onAction: (a) => patch(reply, (m) => ({ ...m, action: { ...a, state: "pending" } })),
        onText: (delta) => patch(reply, (m) => ({ ...m, text: m.text + delta })),
        onDone: (d) => {
          const outcome = resolvedState(trimmed);
          setMessages((all) =>
            all.map((m) => {
              if (m.id === reply)
                return { ...m, text: d.text || m.text, sources: d.sources, metrics: d.metrics, backend: d.backend, streaming: false };
              // An earlier proposal the engine no longer holds was confirmed or cancelled by this turn.
              if (m.action?.state === "pending" && !d.pending_actions.includes(m.action.action_id))
                return { ...m, action: { ...m.action, state: outcome } };
              return m;
            }),
          );
        },
        onError: (message) => patch(reply, (m) => ({ ...m, error: message, streaming: false })),
      });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return onSessionExpired();
      patch(reply, (m) => ({ ...m, error: (e as Error).message, streaming: false }));
    } finally {
      patch(reply, (m) => ({ ...m, streaming: false }));
      setBusy(false);
      onTurnComplete();
      inputRef.current?.focus();
    }
  }

  const suggestions = suggestionsFor(customerId);

  return (
    <section className="flex h-full min-h-0 flex-col">
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6">
        <div className="mx-auto max-w-3xl space-y-5">
          {messages.length === 0 && (
            <div className="pt-6 text-center sm:pt-12">
              <h2 className="text-xl font-semibold">Hi{firstName ? ` ${firstName}` : ""}, how can I help?</h2>
              <p className="mt-1 text-sm text-muted">
                Ask about your EMIs, payments, prepayment or any request. I check your accounts as we talk.
              </p>
            </div>
          )}
          {messages.map((m) => (
            <MessageBubble
              key={m.id}
              message={m}
              showDetails={showDetails}
              busy={busy}
              onConfirm={() => send("Yes, please go ahead.")}
              onCancel={() => send("No, cancel that.")}
            />
          ))}
        </div>
      </div>

      <div className="border-t border-line bg-surface px-4 pb-4 pt-3 sm:px-6">
        <div className="mx-auto max-w-3xl">
          {!busy && (
            <div className="no-scrollbar mb-2 flex gap-2 overflow-x-auto">
              {suggestions.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="shrink-0 rounded-full border border-line bg-surface px-3 py-1 text-xs text-muted hover:border-accent hover:text-accent"
                >
                  {s}
                </button>
              ))}
            </div>
          )}
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
          >
            <textarea
              ref={inputRef}
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send(input);
                }
              }}
              placeholder="Type your question…"
              aria-label="Message"
              className="max-h-32 min-h-[44px] flex-1 resize-none rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[15px] outline-none focus:border-accent focus:ring-2 focus:ring-accent-soft"
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="h-[44px] rounded-xl bg-accent px-4 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-50"
            >
              Send
            </button>
          </form>
        </div>
      </div>
    </section>
  );
}

function resolvedState(userText: string): "confirmed" | "cancelled" {
  return /\b(no|cancel|don't|stop)\b/i.test(userText) ? "cancelled" : "confirmed";
}
