/**
 * Browser speech plumbing for voice mode.
 *
 * Listener: streaming speech recognition (interim words as you speak, a final transcript at the end of
 * each utterance, and the moment speech stopped, for latency).
 * Speaker: speaks a reply sentence by sentence as it streams in. With server TTS it prefetches the next
 * sentence while the current one plays; it falls back to the browser's own voice if the server can't
 * synthesise. stop() silences everything immediately (barge-in).
 */
import { fetchSpeech } from "./api";

/** Rewrite reply text into something that sounds right when read aloud. */
export function speakable(text: string): string {
  return text
    .replace(/\*\*/g, "")
    .replace(/^\s*[-*•]\s+/gm, "")
    .replace(/(?:₹|Rs\.?)\s?(\d[\d,]*\d|\d)(\.\d+)?/g, "$1$2 rupees")
    .replace(/\(policy [A-Z0-9-]+\)/gi, "")
    .replace(/(\d{4})-(\d{2})-(\d{2})/g, (_, y, m, d) =>
      new Date(`${y}-${m}-${d}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "long" }),
    )
    .replace(/\s+/g, " ")
    .trim();
}

/** Splits streamed text into complete sentences so speech can start before the reply is finished. */
export class SentenceSplitter {
  private buffer = "";

  push(delta: string): string[] {
    this.buffer += delta;
    const out: string[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length - 1; i++) {
      const c = this.buffer[i];
      if ((c === "." || c === "!" || c === "?") && /\s/.test(this.buffer[i + 1])) {
        // Don't split after "Rs." (decimals like 10.5 never have a space after the dot).
        if (c === "." && /\bRs$/.test(this.buffer.slice(start, i))) continue;
        const sentence = this.buffer.slice(start, i + 1).trim();
        if (sentence) out.push(sentence);
        start = i + 1;
      }
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  flush(): string[] {
    const rest = this.buffer.trim();
    this.buffer = "";
    return rest ? [rest] : [];
  }
}

type Item = { text: string; filler: boolean; audio?: Promise<Blob | null> };

export class Speaker {
  private queue: Item[] = [];
  private playing = false;
  private current: HTMLAudioElement | null = null;
  private generation = 0;
  spokenText = "";

  constructor(
    private sessionId: string,
    private mode: "openai" | "browser",
    private on: { start: (filler: boolean) => void; idle: () => void },
  ) {}

  get busy(): boolean {
    return this.playing || this.queue.length > 0;
  }

  enqueue(raw: string, filler = false) {
    const text = speakable(raw);
    if (!text) return;
    const item: Item = { text, filler };
    if (this.mode === "openai") item.audio = fetchSpeech(this.sessionId, text).catch(() => null);
    // Once the real answer arrives, fillers that haven't started yet are pointless: drop them.
    if (!filler) this.queue = this.queue.filter((q) => !q.filler);
    this.queue.push(item);
    if (!this.playing) void this.next(this.generation);
  }

  /** Silence immediately and drop anything queued. */
  stop() {
    this.generation++;
    this.queue = [];
    this.playing = false;
    this.current?.pause();
    this.current = null;
    if (typeof window !== "undefined") window.speechSynthesis?.cancel();
  }

  private async next(gen: number) {
    const item = this.queue.shift();
    if (!item) {
      this.playing = false;
      this.on.idle();
      return;
    }
    this.playing = true;
    const blob = item.audio ? await item.audio : null;
    if (gen !== this.generation) return;
    this.spokenText = item.text;
    this.on.start(item.filler);
    if (blob) await this.playBlob(blob, gen);
    else await this.browserSay(item.text, gen);
    if (gen === this.generation) void this.next(gen);
  }

  private playBlob(blob: Blob, gen: number): Promise<void> {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      this.current = audio;
      const done = () => {
        URL.revokeObjectURL(url);
        resolve();
      };
      audio.onended = done;
      audio.onerror = done;
      audio.play().catch(() => (gen === this.generation ? this.browserSay(this.spokenText, gen).then(done) : done()));
    });
  }

  private browserSay(text: string, gen: number): Promise<void> {
    return new Promise((resolve) => {
      const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
      if (!synth || gen !== this.generation) return resolve();
      const u = new SpeechSynthesisUtterance(text);
      const voice = synth.getVoices().find((v) => v.lang === "en-IN") ?? synth.getVoices().find((v) => v.lang.startsWith("en"));
      if (voice) u.voice = voice;
      u.rate = 1.05;
      // Some engines never fire onend (no audio device, voice not loaded); don't let that stall the turn.
      const guard = setTimeout(resolve, 1500 + text.split(/\s+/).length * 450);
      u.onend = () => {
        clearTimeout(guard);
        resolve();
      };
      u.onerror = u.onend;
      synth.speak(u);
    });
  }
}

type RecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onspeechend: (() => void) | null;
  onspeechstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
};

export function speechRecognitionSupported(): boolean {
  return typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);
}

export class Listener {
  private rec: RecognitionLike | null = null;
  private active = false;
  private speechEndedAt: number | null = null;

  constructor(
    private on: {
      interim: (text: string) => void;
      final: (text: string, timing: { speechEndedAt: number | null; finalAt: number }) => void;
      error: (message: string) => void;
    },
  ) {}

  start() {
    const w = window as unknown as Record<string, new () => RecognitionLike>;
    const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
    if (!Ctor) return this.on.error("Voice needs a browser with speech recognition, such as Chrome or Edge.");
    this.active = true;
    const rec = new Ctor();
    rec.lang = "en-IN";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onspeechstart = () => (this.speechEndedAt = null);
    rec.onspeechend = () => (this.speechEndedAt = performance.now());
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) {
          const finalAt = performance.now();
          this.on.final(r[0].transcript.trim(), { speechEndedAt: this.speechEndedAt ?? finalAt, finalAt });
          this.speechEndedAt = null;
        } else interim += r[0].transcript;
      }
      if (interim.trim()) this.on.interim(interim.trim());
    };
    rec.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        this.active = false;
        this.on.error("Microphone access was blocked. Allow it in the browser to use voice.");
      }
    };
    // Recognition stops on its own after silence; keep listening while voice mode is on.
    rec.onend = () => {
      if (this.active) {
        try {
          rec.start();
        } catch {
          /* already restarting */
        }
      }
    };
    this.rec = rec;
    rec.start();
  }

  stop() {
    this.active = false;
    this.rec?.abort();
    this.rec = null;
  }
}
