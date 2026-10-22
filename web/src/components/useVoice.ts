"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getHealth, postVoiceMetrics, setChannel, type VoiceTurnMetrics } from "@/lib/api";
import { Listener, SentenceSplitter, Speaker, speechRecognitionSupported } from "@/lib/speech";

export type VoiceState = "off" | "listening" | "thinking" | "speaking";

export type VoiceTurnHooks = {
  onText: (delta: string) => void;
  onFiller: (text: string) => void;
  onDone: (firstTokenMs: number | null) => void;
  onEnd: (interrupted: boolean) => void;
};

type Props = {
  sessionId: string;
  /** Send a customer utterance through the normal chat pipeline, with voice hooks. */
  send: (text: string, hooks: VoiceTurnHooks) => void;
  /** Abort the in-flight turn (the server cancels it and repairs the conversation). */
  interrupt: () => void;
};

type Timeline = {
  speechEndedAt: number;
  finalAt: number;
  firstAudioAt: number | null;
  firstAnswerAudioAt: number | null;
  firstTokenMs: number | null;
  serverDone: boolean;
  answerStarted: boolean;
};

const BARGE_IN_MIN_WORDS = 2;

export function useVoice({ sessionId, send, interrupt }: Props) {
  const [state, setState] = useState<VoiceState>("off");
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [lastTurn, setLastTurn] = useState<VoiceTurnMetrics | null>(null);
  const [tts, setTts] = useState<"openai" | "browser">("browser");

  const listener = useRef<Listener | null>(null);
  const speaker = useRef<Speaker | null>(null);
  const splitter = useRef(new SentenceSplitter());
  const turn = useRef<Timeline | null>(null);
  const recentlySpoken = useRef<string[]>([]);
  const lastAudioEndAt = useRef(0);
  const handleFinalRef = useRef<(text: string, timing: { speechEndedAt: number | null; finalAt: number }) => void>(() => {});
  const handleInterimRef = useRef<(text: string) => void>(() => {});
  const stopRef = useRef<() => void>(() => {});

  const finishTurn = useCallback(
    (interrupted: boolean) => {
      const t = turn.current;
      if (!t) return;
      turn.current = null;
      const rel = (at: number | null) => (at === null ? null : Math.round(at - t.speechEndedAt));
      const m: VoiceTurnMetrics = {
        transcript_ms: Math.round(t.finalAt - t.speechEndedAt),
        first_audio_ms: rel(t.firstAudioAt),
        first_answer_audio_ms: rel(t.firstAnswerAudioAt),
        first_token_ms: t.firstTokenMs,
        turn_total_ms: Math.round(performance.now() - t.speechEndedAt),
        interrupted,
        tts,
      };
      setLastTurn(m);
      void postVoiceMetrics(sessionId, m);
    },
    [sessionId, tts],
  );

  /** Speech the microphone picked up from our own speaker should not count as the customer talking. */
  const isEcho = useCallback((text: string) => {
    const busy = speaker.current?.busy || performance.now() - lastAudioEndAt.current < 800;
    if (!busy) return false;
    const heard = words(text);
    if (heard.length === 0) return true;
    const ours = new Set(recentlySpoken.current.flatMap(words));
    return heard.filter((w) => ours.has(w)).length / heard.length >= 0.6;
  }, []);

  const bargeIn = useCallback(() => {
    speaker.current?.stop();
    if (turn.current) {
      interrupt();
      finishTurn(true);
    }
    setState("listening");
  }, [interrupt, finishTurn]);

  const handleFinal = useCallback(
    (text: string, timing: { speechEndedAt: number | null; finalAt: number }) => {
      setInterim("");
      if (!text || isEcho(text)) return;
      if (turn.current || speaker.current?.busy) bargeIn();
      splitter.current = new SentenceSplitter();
      recentlySpoken.current = [];
      turn.current = {
        speechEndedAt: timing.speechEndedAt ?? timing.finalAt,
        finalAt: timing.finalAt,
        firstAudioAt: null,
        firstAnswerAudioAt: null,
        firstTokenMs: null,
        serverDone: false,
        answerStarted: false,
      };
      setState("thinking");
      const speak = (sentence: string, filler: boolean) => {
        recentlySpoken.current.push(sentence);
        speaker.current?.enqueue(sentence, filler);
      };
      send(text, {
        onFiller: (f) => {
          if (turn.current && !turn.current.answerStarted) speak(f, true);
        },
        onText: (delta) => {
          for (const sentence of splitter.current.push(delta)) {
            if (turn.current) turn.current.answerStarted = true;
            speak(sentence, false);
          }
        },
        onDone: (firstTokenMs) => {
          for (const sentence of splitter.current.flush()) speak(sentence, false);
          if (!turn.current) return;
          turn.current.firstTokenMs = firstTokenMs;
          turn.current.serverDone = true;
          if (!speaker.current?.busy) {
            finishTurn(false);
            setState("listening");
          }
        },
        onEnd: (interrupted) => {
          if (interrupted || !turn.current) return;
          // Error or empty reply: nothing left to wait for.
          if (!speaker.current?.busy && !turn.current.serverDone) {
            finishTurn(false);
            setState("listening");
          }
        },
      });
    },
    [bargeIn, finishTurn, isEcho, send],
  );

  const handleInterim = useCallback(
    (text: string) => {
      if (isEcho(text)) return;
      setInterim(text);
      // Stop talking as soon as the customer clearly starts speaking; the final transcript follows.
      if ((speaker.current?.busy || turn.current) && words(text).length >= BARGE_IN_MIN_WORDS) bargeIn();
    },
    [bargeIn, isEcho],
  );

  const start = useCallback(async () => {
    setError(null);
    if (!speechRecognitionSupported()) {
      setError("Voice needs a browser with speech recognition, such as Chrome or Edge.");
      return;
    }
    let mode: "openai" | "browser" = "browser";
    try {
      mode = (await getHealth()).tts;
    } catch {
      /* keep browser voice */
    }
    setTts(mode);
    await setChannel(sessionId, "voice").catch(() => undefined);
    speaker.current = new Speaker(sessionId, mode, {
      start: (filler) => {
        const t = turn.current;
        const now = performance.now();
        if (t && t.firstAudioAt === null) t.firstAudioAt = now;
        if (t && !filler && t.firstAnswerAudioAt === null) t.firstAnswerAudioAt = now;
        setState("speaking");
      },
      idle: () => {
        lastAudioEndAt.current = performance.now();
        if (turn.current?.serverDone) {
          finishTurn(false);
          setState("listening");
        } else setState(turn.current ? "thinking" : "listening");
      },
    });
    listener.current = new Listener({
      interim: (t) => handleInterimRef.current(t),
      final: (t, timing) => handleFinalRef.current(t, timing),
      error: (message) => {
        setError(message);
        stopRef.current();
      },
    });
    listener.current.start();
    setState("listening");
  }, [sessionId, finishTurn]);

  const stop = useCallback(() => {
    listener.current?.stop();
    speaker.current?.stop();
    if (turn.current) {
      interrupt();
      finishTurn(true);
    }
    listener.current = null;
    setInterim("");
    setState("off");
    void setChannel(sessionId, "text").catch(() => undefined);
  }, [interrupt, finishTurn, sessionId]);

  // Callbacks change identity as state changes; the long-lived listener calls through refs.
  useEffect(() => {
    handleFinalRef.current = handleFinal;
    handleInterimRef.current = handleInterim;
    stopRef.current = stop;
  });

  useEffect(() => () => {
    listener.current?.stop();
    speaker.current?.stop();
  }, []);

  // Development aid: drive voice turns from the browser console without a microphone.
  useEffect(() => {
    if (process.env.NODE_ENV === "production") return;
    (window as unknown as Record<string, unknown>).__voice = {
      say: (text: string) => handleFinalRef.current(text, { speechEndedAt: performance.now(), finalAt: performance.now() }),
      hear: (text: string) => handleInterimRef.current(text),
    };
  }, []);

  return { state, interim, error, lastTurn, tts, start, stop, stopSpeaking: bargeIn };
}

function words(text: string): string[] {
  return text.toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).filter(Boolean);
}
