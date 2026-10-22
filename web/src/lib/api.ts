import type { Account, DemoCustomer, TurnMetrics, BackendStat } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "Can't reach the service. Check that the API is running.");
  }
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return res.json() as Promise<T>;
}

async function detail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`;
  } catch {
    return `Request failed (${res.status})`;
  }
}

export function getHealth() {
  return request<{ status: string; business_date: string; tts: "openai" | "browser" }>("/api/health");
}

export function setChannel(sessionId: string, channel: "text" | "voice") {
  return request<{ channel: string }>(`/api/sessions/${sessionId}/channel`, {
    method: "POST",
    body: JSON.stringify({ channel }),
  });
}

/** Synthesised speech for one sentence (server TTS). Rejects so the caller can fall back to the browser voice. */
export async function fetchSpeech(sessionId: string, text: string): Promise<Blob> {
  const res = await fetch(`${API_URL}/api/sessions/${sessionId}/tts`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new ApiError(res.status, "Speech unavailable");
  return res.blob();
}

export type VoiceTurnMetrics = {
  transcript_ms: number | null;
  first_audio_ms: number | null;
  first_answer_audio_ms: number | null;
  first_token_ms: number | null;
  turn_total_ms: number | null;
  interrupted: boolean;
  tts: string;
};

export function postVoiceMetrics(sessionId: string, m: VoiceTurnMetrics) {
  return request<{ ok: boolean }>(`/api/sessions/${sessionId}/voice-metrics`, {
    method: "POST",
    body: JSON.stringify(m),
  }).catch(() => undefined);
}

export function getDemoCustomers() {
  return request<{ customers: DemoCustomer[]; otp_hint: string }>("/api/demo-customers");
}

export function login(phone: string, otp: string) {
  return request<{ session_id: string; customer: Account["customer"]; account: Account }>("/api/login", {
    method: "POST",
    body: JSON.stringify({ phone, otp }),
  });
}

export function getAccount(sessionId: string) {
  return request<Account>(`/api/sessions/${sessionId}/account`);
}

export function logout(sessionId: string) {
  return request<{ ok: boolean }>(`/api/sessions/${sessionId}`, { method: "DELETE" }).catch(() => undefined);
}

export type TurnHandlers = {
  onTool: (e: { name: string; label: string }) => void;
  onToolResult: (e: { name: string; ok: boolean; error?: string | null; system?: string | null; sources: string[] }) => void;
  onAction: (e: { action_id: string; summary: string; details: Record<string, unknown> }) => void;
  onFiller?: (text: string) => void;
  onText: (delta: string) => void;
  onDone: (e: {
    text: string;
    metrics: TurnMetrics;
    sources: string[];
    backend: Record<string, BackendStat>;
    refused: boolean;
    pending_actions: string[];
  }) => void;
  onError: (message: string) => void;
};

/** POST a message and dispatch the server-sent events as they arrive (EventSource can't POST). */
export async function sendMessage(sessionId: string, text: string, on: TurnHandlers, signal?: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}/api/sessions/${sessionId}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
      signal,
    });
  } catch (e) {
    if (signal?.aborted) throw e;
    throw new ApiError(0, "Can't reach the service. Check your connection and try again.");
  }
  if (!res.ok || !res.body) throw new ApiError(res.status, await detail(res));

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      dispatch(buffer.slice(0, end), on);
      buffer = buffer.slice(end + 2);
    }
  }
}

function dispatch(block: string, on: TurnHandlers) {
  let event = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data += line.slice(6);
  }
  if (!data) return;
  const payload = JSON.parse(data);
  switch (event) {
    case "tool":
      return on.onTool(payload);
    case "tool_result":
      return on.onToolResult(payload);
    case "action":
      return on.onAction(payload);
    case "filler":
      return on.onFiller?.(payload.text);
    case "text":
      return on.onText(payload.delta);
    case "done":
      return on.onDone(payload);
    case "error":
      return on.onError(payload.message);
  }
}
