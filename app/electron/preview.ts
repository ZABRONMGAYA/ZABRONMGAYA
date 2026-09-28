// The multicamera preview's media server (main process): pictures and sound of project media, decoded by FFmpeg.
//
// The renderer plays what Chromium can decode itself (H.264, HEVC where the graphics hardware decodes it, VP9,
// AAC, WAV…) with <video> elements. Everything else FFmpeg reads (10-bit 4:2:2, ProRes, MXF, DNxHR…) comes from here
// as small JPEG frames: one at a time while scrubbing, or a stream of them at the preview rate while playing, at the
// preview resolution (360p…1080p). The monitored sound comes from here too, as PCM, so every source can be heard
// in step with the master clock. Hardware decoding is used where the FFmpeg build and the computer have it; FFmpeg
// falls back to the processor otherwise. Original media is only read, never written.
import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

export function ffmpegPath(opts: { packaged: boolean; resourcesPath: string }): string {
  const exe = process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg";
  if (opts.packaged) return path.join(opts.resourcesPath, "ffmpeg", exe);
  if (process.env.MCSYNC_FFMPEG_DIR) return path.join(process.env.MCSYNC_FFMPEG_DIR, exe);
  return exe;
}

/** JPEG pictures out of an MJPEG byte stream (each ends with the EOI marker FF D9, which scan data never holds). */
export class JpegSplitter {
  private chunks: Buffer[] = [];
  private pending = Buffer.alloc(0);

  push(data: Buffer): Buffer[] {
    const buf = this.pending.length ? Buffer.concat([this.pending, data]) : data;
    const out: Buffer[] = [];
    let start = 0;
    for (let i = 1; i < buf.length; i++) {
      if (buf[i - 1] === 0xff && buf[i] === 0xd9) {
        out.push(buf.subarray(start, i + 1));
        start = i + 1;
      }
    }
    this.pending = Buffer.from(buf.subarray(start));
    this.chunks = [];
    return out;
  }
}

/** A bounded cache of pictures and sound, least recently used first out. */
class Lru<V extends { length: number }> {
  private map = new Map<string, V>();
  private bytes = 0;
  constructor(private readonly limit: number) {}
  get(key: string): V | undefined {
    const v = this.map.get(key);
    if (v !== undefined) {
      this.map.delete(key);
      this.map.set(key, v);
    }
    return v;
  }
  set(key: string, value: V): void {
    const old = this.map.get(key);
    if (old) this.bytes -= old.length;
    this.map.delete(key);
    this.map.set(key, value);
    this.bytes += value.length;
    for (const [k, v] of this.map) {
      if (this.bytes <= this.limit) break;
      this.map.delete(k);
      this.bytes -= v.length;
    }
  }
  clear(): void {
    this.map.clear();
    this.bytes = 0;
  }
}

interface Stream {
  proc: ChildProcess;
  start: number; // source time of the first frame
  fps: number;
  frames: { t: number; jpeg: Buffer }[];
  next: number; // index of the next frame to arrive
  wanted: number; // latest source time asked for
  ended: boolean;
  paused: boolean;
  lastUse: number;
}

export interface PreviewCaps {
  hwaccels: string[];
  ffmpeg: string;
}

const AHEAD_S = 3; // frames decoded ahead of the playhead before FFmpeg is made to wait
const MAX_FRAME_JOBS = 6; // FFmpeg processes for single frames at once
const STREAM_IDLE_MS = 15_000;

export class PreviewServer {
  private frames = new Lru<Buffer>(256 * 2 ** 20);
  private streams = new Map<string, Stream>();
  private nextId = 1;
  private running = 0;
  private queue: { slot: string; run: () => void; drop: () => void }[] = [];
  private caps: Promise<PreviewCaps> | null = null;
  private noHw = new Set<string>(); // files FFmpeg could not decode with hardware
  private sweeper: NodeJS.Timeout;

  constructor(
    private readonly ffmpeg: string,
    private readonly allowed: (file: string) => boolean,
  ) {
    this.sweeper = setInterval(() => this.sweep(), 5000);
    this.sweeper.unref?.();
  }

  capabilities(): Promise<PreviewCaps> {
    this.caps ??= new Promise((resolve) => {
      const proc = spawn(this.ffmpeg, ["-hide_banner", "-hwaccels"], { windowsHide: true });
      let out = "";
      proc.stdout.on("data", (d: Buffer) => (out += d.toString()));
      proc.on("error", () => resolve({ hwaccels: [], ffmpeg: this.ffmpeg }));
      proc.on("close", () =>
        resolve({
          hwaccels: out
            .split(/\r?\n/)
            .slice(1)
            .map((s) => s.trim())
            .filter(Boolean),
          ffmpeg: this.ffmpeg,
        }),
      );
    });
    return this.caps;
  }

  private check(file: string): string {
    const full = path.resolve(file);
    if (!this.allowed(full)) throw new Error("not a media file of the open project");
    return full;
  }

  private hwArgs(file: string): string[] {
    return this.noHw.has(file) ? [] : ["-hwaccel", "auto"];
  }

  /** One picture of `file` at source time `t` (seconds), `height` pixels high. Requests for the same `slot`
   * (a camera window) replace each other while waiting: scrubbing asks for the latest position only. */
  async frame(file: string, t: number, height: number, slot: string): Promise<Buffer | null> {
    const full = this.check(file);
    const key = `${full}|${height}|${Math.round(t * 10000)}`; // the player asks for frame starts
    const cached = this.frames.get(key);
    if (cached) return cached;
    return new Promise((resolve) => {
      for (const job of this.queue.filter((j) => j.slot === slot)) job.drop();
      this.queue = this.queue.filter((j) => j.slot !== slot);
      const run = () => {
        this.running += 1;
        this.decodeFrame(full, t, height).then(
          (jpeg) => {
            if (jpeg) this.frames.set(key, jpeg);
            resolve(jpeg);
          },
          () => resolve(null),
        );
      };
      this.queue.push({ slot, run, drop: () => resolve(null) });
      this.pump();
    });
  }

  private pump(): void {
    while (this.running < MAX_FRAME_JOBS && this.queue.length) this.queue.shift()!.run();
  }

  private decodeFrame(file: string, t: number, height: number, hw = true): Promise<Buffer | null> {
    const args = [
      "-hide_banner", "-v", "error", "-nostdin",
      ...(hw ? this.hwArgs(file) : []),
      "-ss", Math.max(0, t).toFixed(4), "-i", file,
      "-frames:v", "1", "-an", "-sn", "-dn",
      "-vf", `scale=-2:${height}:flags=bilinear`,
      "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1",
    ]; // prettier-ignore
    return new Promise((resolve) => {
      const proc = spawn(this.ffmpeg, args, { windowsHide: true });
      const parts: Buffer[] = [];
      proc.stdout.on("data", (d: Buffer) => parts.push(d));
      proc.on("error", () => resolve(null));
      proc.on("close", (code) => {
        this.running -= 1;
        this.pump();
        const jpeg = Buffer.concat(parts);
        if (jpeg.length) resolve(jpeg);
        else if (hw && code !== 0 && !this.noHw.has(file)) {
          this.noHw.add(file); // hardware decoding failed for this file: the processor decodes it from now on
          this.running += 1;
          resolve(this.decodeFrame(file, t, height, false));
        } else resolve(null);
      });
    });
  }

  /** Start decoding `file` from source time `start` at `fps` frames per second, `height` pixels high. */
  open(file: string, start: number, fps: number, height: number): string {
    const full = this.check(file);
    const id = `s${this.nextId++}`;
    const args = [
      "-hide_banner", "-v", "error", "-nostdin",
      ...this.hwArgs(full),
      "-ss", Math.max(0, start).toFixed(4), "-i", full,
      "-an", "-sn", "-dn",
      "-vf", `fps=${fps},scale=-2:${height}:flags=bilinear`,
      "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6", "pipe:1",
    ]; // prettier-ignore
    const proc = spawn(this.ffmpeg, args, { windowsHide: true });
    const stream: Stream = {
      proc,
      start: Math.max(0, start),
      fps,
      frames: [],
      next: 0,
      wanted: start,
      ended: false,
      paused: false,
      lastUse: Date.now(),
    };
    const splitter = new JpegSplitter();
    proc.stdout!.on("data", (d: Buffer) => {
      for (const jpeg of splitter.push(d)) {
        stream.frames.push({ t: stream.start + stream.next / fps, jpeg });
        stream.next += 1;
      }
      this.throttle(stream);
    });
    proc.on("close", () => (stream.ended = true));
    proc.on("error", () => (stream.ended = true));
    this.streams.set(id, stream);
    return id;
  }

  /** Keep a stream no more than a few seconds ahead of what is shown (FFmpeg waits while its output is not read). */
  private throttle(s: Stream): void {
    const ahead = s.frames.length ? s.frames[s.frames.length - 1]!.t - s.wanted : 0;
    if (!s.paused && ahead > AHEAD_S) {
      s.proc.stdout?.pause();
      s.paused = true;
    } else if (s.paused && ahead < AHEAD_S / 2) {
      s.proc.stdout?.resume();
      s.paused = false;
    }
  }

  /** The stream's picture for source time `t`: a JPEG, `null` when not decoded yet, or "reopen" when `t` is
   * outside what this stream can give (the playhead jumped). */
  at(id: string, t: number): Buffer | null | "reopen" {
    const s = this.streams.get(id);
    if (!s) return "reopen";
    s.lastUse = Date.now();
    s.wanted = t;
    const half = 0.5 / s.fps;
    if (t < s.start - 0.25 || (s.frames.length && t < s.frames[0]!.t - 1)) return "reopen";
    // Frames before the one on screen are no longer needed.
    while (s.frames.length > 1 && s.frames[1]!.t <= t + half) s.frames.shift();
    this.throttle(s);
    const f = s.frames[0];
    if (!f) return s.ended ? "reopen" : null;
    if (f.t > t + half) return null; // not there yet
    if (!s.ended && t - f.t > 2) return "reopen"; // far behind: decoding cannot keep up at this position
    return f.jpeg;
  }

  close(id: string): void {
    const s = this.streams.get(id);
    if (!s) return;
    this.streams.delete(id);
    if (!s.ended) s.proc.kill();
  }

  /** `seconds` of sound of `file` from `start`, as interleaved stereo float32 at `rate` Hz (the audio monitor). */
  audio(file: string, start: number, seconds: number, rate: number, stream: number | null): Promise<Buffer | null> {
    const full = this.check(file);
    const args = [
      "-hide_banner", "-v", "error", "-nostdin",
      "-ss", Math.max(0, start).toFixed(4), "-t", seconds.toFixed(4), "-i", full,
      "-map", stream === null ? "0:a:0?" : `0:${stream}`, "-vn", "-sn", "-dn",
      "-ac", "2", "-ar", String(rate), "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1",
    ]; // prettier-ignore
    return new Promise((resolve) => {
      const proc = spawn(this.ffmpeg, args, { windowsHide: true });
      const parts: Buffer[] = [];
      proc.stdout.on("data", (d: Buffer) => parts.push(d));
      proc.on("error", () => resolve(null));
      proc.on("close", () => resolve(parts.length ? Buffer.concat(parts) : null));
    });
  }

  private sweep(): void {
    const now = Date.now();
    for (const [id, s] of this.streams) if (now - s.lastUse > STREAM_IDLE_MS) this.close(id);
  }

  /** The project closed: stop everything and forget its pictures. */
  reset(): void {
    for (const id of [...this.streams.keys()]) this.close(id);
    for (const job of this.queue) job.drop();
    this.queue = [];
    this.frames.clear();
    this.noHw.clear();
  }

  dispose(): void {
    clearInterval(this.sweeper);
    this.reset();
  }
}
