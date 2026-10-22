import { seconds } from "@/lib/format";
import type { VoiceTurnMetrics } from "@/lib/api";
import type { VoiceState } from "./useVoice";

const LABEL: Record<VoiceState, string> = {
  off: "",
  listening: "Listening… just start talking",
  thinking: "Checking…",
  speaking: "Speaking. Talk any time to interrupt",
};

type Props = {
  state: VoiceState;
  interim: string;
  lastTurn: VoiceTurnMetrics | null;
  showDetails: boolean;
  tts: string;
  onStopSpeaking: () => void;
  onEnd: () => void;
};

export default function VoiceBar({ state, interim, lastTurn, showDetails, tts, onStopSpeaking, onEnd }: Props) {
  return (
    <div className="rounded-2xl border border-accent/40 bg-accent-soft px-4 py-3">
      <div className="flex items-center gap-3">
        <Orb state={state} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-accent">{LABEL[state]}</p>
          <p className="truncate text-sm text-muted">{interim || " "}</p>
        </div>
        {state === "speaking" && (
          <button
            onClick={onStopSpeaking}
            className="rounded-lg border border-line bg-surface px-3 py-1.5 text-xs hover:border-accent"
          >
            Stop
          </button>
        )}
        <button onClick={onEnd} className="rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent-strong">
          End voice
        </button>
      </div>
      {showDetails && lastTurn && (
        <p className="mt-2 font-mono text-[11px] text-muted">
          last turn: transcript {seconds(lastTurn.transcript_ms)} · first sound {seconds(lastTurn.first_audio_ms)} · answer
          audio {seconds(lastTurn.first_answer_audio_ms)} · first token {seconds(lastTurn.first_token_ms)}
          {lastTurn.interrupted ? " · interrupted" : ""} · {tts} voice
        </p>
      )}
      <p className="mt-1 text-[11px] text-muted">Tip: use headphones so the assistant doesn&apos;t hear itself.</p>
    </div>
  );
}

function Orb({ state }: { state: VoiceState }) {
  const ring = {
    off: "",
    listening: "animate-pulse",
    thinking: "animate-spin border-t-transparent",
    speaking: "animate-ping",
  }[state];
  return (
    <span className="relative grid h-9 w-9 shrink-0 place-items-center">
      <span className={`absolute inset-0 rounded-full border-2 border-accent ${ring}`} />
      <span className="h-3 w-3 rounded-full bg-accent" />
    </span>
  );
}
