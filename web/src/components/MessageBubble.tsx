"use client";

import { useState } from "react";
import RichText from "./RichText";
import { SYSTEM_LABEL, humanize, seconds, sourceLabel } from "@/lib/format";
import type { ChatMessage } from "@/lib/types";

type Props = {
  message: ChatMessage;
  showDetails: boolean;
  busy: boolean;
  onConfirm: (actionId: string) => void;
  onCancel: (actionId: string) => void;
};

export default function MessageBubble({ message, showDetails, busy, onConfirm, onCancel }: Props) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-[15px] text-white">
          {message.text}
        </div>
      </div>
    );
  }

  const failedSystems = [...new Set(message.activities.filter((a) => a.error === "SYSTEM_UNAVAILABLE").map((a) => a.system))];
  const thinking = message.streaming && !message.text && !message.interrupted;

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 h-7 w-7 shrink-0 rounded-lg bg-accent-soft text-accent grid place-items-center text-xs font-semibold">
        AI
      </div>
      <div className="min-w-0 max-w-[85%] flex-1 space-y-2">
        {message.activities.length > 0 && (
          <ul className="space-y-1">
            {dedupe(message.activities).map((a, i) => (
              <li key={i} className="flex items-center gap-2 text-xs text-muted">
                <StatusDot status={a.status} />
                <span>{a.label}</span>
                {a.status === "error" && a.error && a.error !== "NOT_ELIGIBLE" && (
                  <span className="text-warn">· {a.error === "SYSTEM_UNAVAILABLE" ? "system unavailable" : humanize(a.error)}</span>
                )}
              </li>
            ))}
          </ul>
        )}

        {(message.text || thinking || message.error || message.interrupted) && (
          <div className="rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-3 text-[15px]">
            {thinking ? <TypingDots /> : <RichText text={message.text} />}
            {message.interrupted && <p className="mt-1 text-xs text-muted">Interrupted</p>}
            {message.error && <p className="mt-2 text-sm text-bad">{message.error}</p>}
          </div>
        )}

        {failedSystems.length > 0 && !message.streaming && (
          <p className="rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
            {failedSystems.map((s) => SYSTEM_LABEL[s ?? ""] ?? s).join(", ")} temporarily unavailable. This answer may be partial.
          </p>
        )}

        {message.action && (
          <ActionCard action={message.action} busy={busy} onConfirm={onConfirm} onCancel={onCancel} />
        )}

        {!message.streaming && message.sources.length > 0 && <Sources sources={message.sources} />}

        {showDetails && message.metrics && (
          <div className="rounded-lg bg-surface-2 px-3 py-2 font-mono text-[11px] leading-5 text-muted">
            <div>
              first word {seconds(message.metrics.first_text_ms)} · total {seconds(message.metrics.total_ms)} · model{" "}
              {seconds(message.metrics.llm_ms)} · tools {seconds(message.metrics.tools_ms)} · {message.metrics.steps} step
              {message.metrics.steps === 1 ? "" : "s"}
            </div>
            {message.backend && Object.keys(message.backend).length > 0 && (
              <div>
                {Object.entries(message.backend)
                  .map(
                    ([sys, b]) =>
                      `${sys} ${b.calls}× max ${seconds(b.ms)}${b.retries ? ` (${b.retries} retry)` : ""}${b.failed ? " FAILED" : ""}`,
                  )
                  .join(" · ")}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ActionCard({
  action,
  busy,
  onConfirm,
  onCancel,
}: {
  action: NonNullable<ChatMessage["action"]>;
  busy: boolean;
  onConfirm: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  return (
    <div className="rounded-xl border border-accent/40 bg-accent-soft p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-accent">Needs your confirmation</p>
      <p className="mt-1 text-sm">{action.summary}</p>
      {action.state === "pending" ? (
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => onConfirm(action.action_id)}
            disabled={busy}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-60"
          >
            Confirm
          </button>
          <button
            onClick={() => onCancel(action.action_id)}
            disabled={busy}
            className="rounded-lg border border-line bg-surface px-3 py-1.5 text-sm hover:border-accent disabled:opacity-60"
          >
            Cancel
          </button>
        </div>
      ) : (
        <p className={`mt-2 text-xs font-medium ${action.state === "confirmed" ? "text-ok" : "text-muted"}`}>
          {action.state === "confirmed" ? "Confirmed" : "Cancelled"}
        </p>
      )}
    </div>
  );
}

function Sources({ sources }: { sources: string[] }) {
  const [open, setOpen] = useState(false);
  const policies = sources.filter((s) => s.startsWith("policy:"));
  const records = sources.filter((s) => !s.startsWith("policy:"));
  return (
    <div className="text-xs">
      <button onClick={() => setOpen(!open)} className="text-muted hover:text-accent">
        {open ? "Hide" : "Based on"} {records.length} record{records.length === 1 ? "" : "s"}
        {policies.length > 0 && ` and ${policies.length} polic${policies.length === 1 ? "y" : "ies"}`}
        {open ? "" : " ›"}
      </button>
      {open && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {[...policies, ...records].map((s) => (
            <span
              key={s}
              className={`rounded-md px-2 py-0.5 ${s.startsWith("policy:") ? "bg-accent-soft text-accent" : "bg-surface-2 text-muted"}`}
            >
              {sourceLabel(s)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function StatusDot({ status }: { status: "running" | "ok" | "error" | "stopped" }) {
  if (status === "running")
    return <span className="h-3 w-3 rounded-full border-2 border-accent border-t-transparent animate-spin" />;
  const color = { ok: "bg-ok", error: "bg-warn", stopped: "bg-muted" }[status];
  return <span className={`h-2 w-2 rounded-full ${color}`} />;
}

function TypingDots() {
  return (
    <span className="inline-flex gap-1 py-1" aria-label="Assistant is typing">
      {[0, 1, 2].map((i) => (
        <span key={i} className="typing-dot h-1.5 w-1.5 rounded-full bg-muted" style={{ animationDelay: `${i * 0.15}s` }} />
      ))}
    </span>
  );
}

/** Repeated calls to the same tool in one turn show once, with the latest status. */
function dedupe(activities: ChatMessage["activities"]) {
  const seen = new Map<string, ChatMessage["activities"][number]>();
  for (const a of activities) seen.set(a.label, a);
  return [...seen.values()];
}
