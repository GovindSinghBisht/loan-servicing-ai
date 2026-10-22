"use client";

import { useEffect, useRef, useState } from "react";
import MessageBubble from "./MessageBubble";
import VoiceBar from "./VoiceBar";
import { useVoice, type VoiceTurnHooks } from "./useVoice";
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
  const inFlight = useRef<AbortController | null>(null);
  const voice = useVoice({
    sessionId,
    send: (text, hooks) => void send(text, hooks),
    interrupt: () => inFlight.current?.abort(),
  });
  const voiceOn = voice.state !== "off";

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  function patch(id: string, update: (m: ChatMessage) => ChatMessage) {
    setMessages((all) => all.map((m) => (m.id === id ? update(m) : m)));
  }

  async function send(text: string, hooks?: VoiceTurnHooks) {
    const trimmed = text.trim();
    // Voice turns may start while a reply is in flight: the voice layer interrupts it first.
    if (!trimmed || (busy && !hooks)) return;
    const ctrl = new AbortController();
    inFlight.current = ctrl;
    setBusy(true);
    if (!hooks) setInput("");
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
        onFiller: (f) => hooks?.onFiller(f),
        onText: (delta) => {
          hooks?.onText(delta);
          patch(reply, (m) => ({ ...m, text: m.text + delta }));
        },
        onDone: (d) => {
          hooks?.onDone(d.metrics.first_text_ms);
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
      }, ctrl.signal);
    } catch (e) {
      if (ctrl.signal.aborted) {
        patch(reply, (m) => ({
          ...m,
          interrupted: true,
          streaming: false,
          activities: m.activities.map((a) => (a.status === "running" ? { ...a, status: "stopped" } : a)),
        }));
      } else if (e instanceof ApiError && e.status === 404) {
        return onSessionExpired();
      } else {
        patch(reply, (m) => ({ ...m, error: (e as Error).message, streaming: false }));
      }
    } finally {
      patch(reply, (m) => ({ ...m, streaming: false }));
      hooks?.onEnd(ctrl.signal.aborted);
      // A newer turn (barge-in) owns the busy state now.
      if (inFlight.current === ctrl) {
        inFlight.current = null;
        setBusy(false);
        onTurnComplete();
        if (!hooks) inputRef.current?.focus();
      }
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
          {voice.error && <p className="mb-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">{voice.error}</p>}
          {voiceOn ? (
            <VoiceBar
              state={voice.state}
              interim={voice.interim}
              lastTurn={voice.lastTurn}
              showDetails={showDetails}
              tts={voice.tts}
              onStopSpeaking={voice.stopSpeaking}
              onEnd={voice.stop}
            />
          ) : (
          <>
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
              type="button"
              onClick={() => void voice.start()}
              disabled={busy}
              aria-label="Talk to the assistant"
              title="Talk to the assistant"
              className="grid h-[44px] w-[44px] place-items-center rounded-xl border border-line bg-surface text-accent hover:border-accent disabled:opacity-50"
            >
              <MicIcon />
            </button>
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="h-[44px] rounded-xl bg-accent px-4 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-50"
            >
              Send
            </button>
          </form>
          </>
          )}
        </div>
      </div>
    </section>
  );
}

function MicIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

function resolvedState(userText: string): "confirmed" | "cancelled" {
  return /\b(no|cancel|don't|stop)\b/i.test(userText) ? "cancelled" : "confirmed";
}
