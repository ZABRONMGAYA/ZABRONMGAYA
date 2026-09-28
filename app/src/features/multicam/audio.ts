// The audio monitor: one source heard at a time (never every camera at once: echoes and phasing), decoded by
// FFmpeg in the main process and scheduled with Web Audio against the master clock, so what is heard is exactly
// where the pictures are, whatever the file's codec.
import { bridge } from "../../api/client";
import { clock } from "./clock";

export interface MonitorSource {
  path: string;
  /** Absolute stream index of the audio to hear (null: the file's first audio stream). */
  stream: number | null;
  /** Source time at master time `masterT`. */
  t: number;
  /** Master time at which this clip ends. */
  clipEnd: number;
  /** Clock rate of the source relative to the master clock (1 + drift). */
  rate: number;
}

/** Where the monitored source is at master time `t`: a clip and its position, or the next time it has media. */
export type MonitorResolver = (t: number) => { source: MonitorSource } | { nextAt: number | null };

const CHUNK_S = 2;
const LOOKAHEAD_S = 3;
const RATE = 48000;

export class AudioMonitor {
  private ctx: AudioContext | null = null;
  private gain: GainNode | null = null;
  private resolver: MonitorResolver | null = null;
  private nodes: AudioBufferSourceNode[] = [];
  private scheduledUntil = 0; // master time up to which sound is scheduled
  private lastEnd: { master: number; ctx: number } | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private token = 0;
  private busy = false;
  private off: (() => void) | null = null;
  volume = 1;
  /** Chunks scheduled so far and the file of the last one (diagnostics and tests). */
  chunks = 0;
  lastPath: string | null = null;

  constructor() {
    this.off = clock.subscribe((e) => {
      if (e === "play") this.start();
      else this.stop();
      if (e === "seek" && clock.playing) this.start();
    });
  }

  /** What to hear (null: nothing). Takes effect at once. */
  setSource(resolver: MonitorResolver | null): void {
    this.resolver = resolver;
    this.stop();
    if (clock.playing) this.start();
  }

  private start(): void {
    if (!this.resolver || clock.rate !== 1) return;
    this.ctx ??= new AudioContext({ sampleRate: RATE, latencyHint: "interactive" });
    if (!this.gain) {
      this.gain = this.ctx.createGain();
      this.gain.connect(this.ctx.destination);
    }
    this.gain.gain.value = this.volume;
    void this.ctx.resume();
    this.token += 1;
    this.scheduledUntil = clock.now();
    this.lastEnd = null;
    this.timer ??= setInterval(() => void this.fill(), 200);
    void this.fill();
  }

  private stop(): void {
    this.token += 1;
    for (const n of this.nodes) {
      try {
        n.stop();
      } catch {
        // already ended
      }
    }
    this.nodes = [];
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private async fill(): Promise<void> {
    if (this.busy || !this.ctx || !this.resolver || !clock.playing) return;
    const token = this.token;
    this.busy = true;
    try {
      while (token === this.token && clock.playing && this.scheduledUntil < clock.now() + LOOKAHEAD_S) {
        const m = Math.max(this.scheduledUntil, clock.now());
        const found = this.resolver(m);
        if (!("source" in found)) {
          // A gap in this source: nothing to hear until its next clip.
          this.scheduledUntil = found.nextAt ?? m + LOOKAHEAD_S;
          this.lastEnd = null;
          continue;
        }
        const src = found.source;
        const dur = Math.min(CHUNK_S, src.clipEnd - m);
        if (dur <= 0.01) {
          this.scheduledUntil = src.clipEnd;
          continue;
        }
        const pcm = await bridge().preview.audio(src.path, src.t, dur * src.rate, RATE, src.stream);
        if (token !== this.token || !this.ctx) return;
        this.scheduledUntil = m + dur;
        if (!pcm || pcm.length < 2) continue;
        this.schedule(pcm, m, dur);
        this.chunks++;
        this.lastPath = src.path;
      }
    } finally {
      this.busy = false;
    }
  }

  private schedule(pcm: Float32Array, masterStart: number, dur: number): void {
    const ctx = this.ctx!;
    const frames = pcm.length >> 1;
    const buffer = ctx.createBuffer(2, frames, RATE);
    const left = buffer.getChannelData(0);
    const right = buffer.getChannelData(1);
    for (let i = 0; i < frames; i++) {
      left[i] = pcm[2 * i]!;
      right[i] = pcm[2 * i + 1]!;
    }
    // Where this chunk starts on the audio clock: seamless after the previous chunk, re-anchored on the master
    // clock when the two have drifted more than 15 ms apart.
    let at = ctx.currentTime + (masterStart - clock.now());
    if (this.lastEnd && Math.abs(this.lastEnd.master - masterStart) < 1e-3 && Math.abs(this.lastEnd.ctx - at) < 0.015)
      at = this.lastEnd.ctx;
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.playbackRate.value = frames / RATE / dur; // the source's own clock rate (drift)
    node.connect(this.gain!);
    const late = ctx.currentTime - at;
    if (late > 0) {
      if (late >= dur) return;
      node.start(ctx.currentTime, late);
    } else node.start(at);
    this.nodes.push(node);
    node.onended = () => {
      this.nodes = this.nodes.filter((n) => n !== node);
    };
    this.lastEnd = { master: masterStart + dur, ctx: at + dur };
  }

  setVolume(v: number): void {
    this.volume = v;
    if (this.gain) this.gain.gain.value = v;
  }

  dispose(): void {
    this.stop();
    this.off?.();
    void this.ctx?.close();
    this.ctx = null;
  }
}
