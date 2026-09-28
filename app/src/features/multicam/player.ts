// One camera window's player: shows a clip at the source time the master clock asks for. Where Chromium decodes
// the file it plays natively (hardware decoding, muted: sound comes from the audio monitor) and is steered onto
// the master clock every frame; otherwise pictures come from FFmpeg in the main process (single frames while
// paused or scrubbing, a stream while playing). It never keeps its own clock, and it never holds a last frame
// once its clip has ended: the caller shows the gap instead.
import type { TimelineClip } from "../../api/contract";
import { bridge } from "../../api/client";

export type PlayerMode = "native" | "frames" | "none";

/** Files Chromium could not decode (this session): FFmpeg frames from the start. */
const nativeFailed = new Set<string>();

/** Seek instead of steering when the picture is this far off the clock (seconds). */
const SEEK_S = 0.2;
/** Steering gain: playback rate correction per second of error, bounded to ±25 % (the picture is muted, so a
 * brief speed change is not heard; 100 ms of error is gone in half a second). */
const GAIN = 2;
const MAX_CORRECTION = 0.25;

export interface PlayerSettings {
  /** Native decoding when possible (false: FFmpeg frames at `height`). */
  preferNative: boolean;
  /** Picture height for FFmpeg frames. */
  height: number;
  /** Frames per second of FFmpeg streams. */
  fps: number;
}

export class TilePlayer {
  private clip: TimelineClip | null = null;
  private mode: PlayerMode = "none";
  private stream: { id: string; path: string; opening: boolean } | null = null;
  private pending = false;
  private wantT: number | null = null; // newest paused request waiting for the one in flight
  private shownT = Number.NaN; // source time of the picture on screen (frames mode)
  private drawn = 0;
  private windowStart = performance.now();
  private disposed = false;
  private frameCallback = 0;
  private last: { t: number; playing: boolean; rate: number } | null = null;
  /** Pictures shown per second over the last second. */
  fps = 0;
  settings: PlayerSettings = { preferNative: true, height: 540, fps: 25 };
  onChange: ((mode: PlayerMode, fps: number) => void) | null = null;

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly canvas: HTMLCanvasElement,
    private readonly slot: string,
  ) {
    video.muted = true;
    video.crossOrigin = "anonymous"; // the media protocol allows it: pictures can be read back (tests, scopes)
    video.playsInline = true;
    video.preload = "auto";
    video.addEventListener("error", this.onNativeError);
    video.addEventListener("loadedmetadata", this.onMetadata);
    this.countNativeFrames();
  }

  get current(): PlayerMode {
    return this.mode;
  }

  /** Show `clip` at source time `t` (null: nothing). Call every animation frame while playing and after seeks. */
  update(clip: TimelineClip | null, t: number, playing: boolean, rate: number): void {
    if (this.disposed) return;
    this.last = { t, playing, rate };
    if (clip?.clip_id !== this.clip?.clip_id || clip?.path !== this.clip?.path) this.load(clip, t);
    if (!clip) return;
    if (this.mode === "native") this.steer(t, playing, rate * (1 + clip.drift_ppm * 1e-6));
    else if (this.mode === "frames") this.frames(t, playing);
    this.tick();
  }

  /** Settings changed (quality, window size): reload the current clip the new way. */
  configure(settings: PlayerSettings, t: number | null): void {
    const changed =
      settings.preferNative !== this.settings.preferNative ||
      (this.mode === "frames" && (settings.height !== this.settings.height || settings.fps !== this.settings.fps));
    this.settings = settings;
    if (changed && this.clip) {
      const clip = this.clip;
      this.load(null, 0);
      this.load(clip, t ?? 0);
      this.refresh();
    }
  }

  dispose(): void {
    this.disposed = true;
    this.load(null, 0);
    this.video.removeEventListener("error", this.onNativeError);
    this.video.removeEventListener("loadedmetadata", this.onMetadata);
    if (this.frameCallback && "cancelVideoFrameCallback" in this.video)
      this.video.cancelVideoFrameCallback(this.frameCallback);
  }

  // ------------------------------------------------------------------ loading

  private load(clip: TimelineClip | null, t: number): void {
    this.closeStream();
    this.clip = clip;
    this.shownT = Number.NaN;
    this.wantT = null;
    const v = this.video;
    if (!clip || !clip.has_video) {
      if (v.getAttribute("src")) {
        v.pause();
        v.removeAttribute("src");
        v.load();
      }
      this.clear();
      this.setMode("none");
      return;
    }
    if (this.settings.preferNative && !nativeFailed.has(clip.path)) {
      this.clear();
      v.src = bridge().mediaUrl(clip.path);
      try {
        v.currentTime = Math.max(0, t);
      } catch {
        // set again once metadata is known
      }
      this.setMode("native");
    } else {
      if (v.getAttribute("src")) {
        v.pause();
        v.removeAttribute("src");
        v.load();
      }
      this.setMode("frames");
    }
  }

  private onNativeError = (): void => {
    const clip = this.clip;
    if (!clip || this.mode !== "native") return;
    nativeFailed.add(clip.path); // e.g. ProRes, DNxHR: FFmpeg decodes it
    const t = this.last?.t ?? this.video.currentTime;
    this.load(null, 0);
    this.load(clip, t);
    this.refresh();
  };

  /** Show the picture for the last position asked for, now (after a reload: the clock may be paused). */
  private refresh(): void {
    const last = this.last;
    if (last && this.clip) this.update(this.clip, last.t, last.playing, last.rate);
  }

  private onMetadata = (): void => {
    // Some containers load but carry a video track Chromium cannot decode: no picture size.
    if (this.mode === "native" && this.clip?.has_video && this.video.videoWidth === 0) this.onNativeError();
  };

  // ------------------------------------------------------------------ native

  private steer(t: number, playing: boolean, rate: number): void {
    const v = this.video;
    if (v.readyState < 1) return; // no metadata yet: the seek set on load applies
    const err = v.currentTime - t; // positive: the picture is ahead of the clock
    if (playing) {
      if (Math.abs(err) > SEEK_S) {
        if (!v.seeking) v.currentTime = Math.max(0, t + 0.05); // lead the clock by the time a seek takes
      } else {
        const correction = Math.max(-MAX_CORRECTION, Math.min(MAX_CORRECTION, -err * GAIN));
        v.playbackRate = rate * (1 + correction);
      }
      if (v.paused) void v.play().catch(() => undefined);
    } else {
      const stopping = !v.paused;
      if (stopping) v.pause();
      // Stopping: always seek once, so the picture is the frame at t (a paused <video> keeps the last frame it
      // presented, which can be ahead of its position after steering); afterwards, only when it is off.
      const frame = 1 / (this.clipFps() || 25);
      if ((stopping || Math.abs(err) > frame / 2) && !v.seeking) v.currentTime = Math.max(0, t);
    }
  }

  private countNativeFrames(): void {
    const v = this.video;
    if (!("requestVideoFrameCallback" in v)) return;
    const onFrame = () => {
      if (this.disposed) return;
      if (this.mode === "native") this.drawn++;
      this.frameCallback = v.requestVideoFrameCallback(onFrame);
    };
    this.frameCallback = v.requestVideoFrameCallback(onFrame);
  }

  // ------------------------------------------------------------------ frames

  private frames(t: number, playing: boolean): void {
    const clip = this.clip!;
    if (!playing) {
      this.closeStream();
      // The frame a <video> shows at t: the one whose display interval contains t (its start on the clip's grid).
      const fps = this.clipFps() || 25;
      const tq = Math.floor(t * fps + 1e-6) / fps;
      if (tq === this.shownT) return;
      if (this.pending) {
        this.wantT = tq;
        return;
      }
      this.request(this.exactFrame(clip, tq), tq);
      return;
    }
    if (!this.stream || this.stream.path !== clip.path) {
      this.openStream(clip.path, t);
      return;
    }
    if (this.stream.opening || this.pending) return;
    const step = 1 / this.settings.fps;
    if (Math.floor(t / step) === Math.floor(this.shownT / step)) return;
    const stream = this.stream;
    this.pending = true;
    bridge()
      .preview.at(stream.id, t)
      .then(
        (jpeg) => {
          this.pending = false;
          if (this.disposed || this.stream !== stream) return;
          if (jpeg === "reopen") this.openStream(clip.path, t);
          else if (jpeg) void this.draw(jpeg, t);
        },
        () => {
          this.pending = false;
        },
      );
  }

  private request(frame: Promise<Uint8Array | null>, t: number): void {
    const clip = this.clip;
    this.pending = true;
    frame.then(
      (jpeg) => {
        this.pending = false;
        if (this.disposed || this.clip !== clip) return;
        if (jpeg) void this.draw(jpeg, t);
        const next = this.wantT;
        this.wantT = null;
        if (next !== null && clip) this.request(this.exactFrame(clip, next), next);
      },
      () => {
        this.pending = false;
      },
    );
  }

  private openStream(path: string, t: number): void {
    this.closeStream();
    const stream = { id: "", path, opening: true };
    this.stream = stream;
    bridge()
      .preview.open(path, Math.max(0, t), this.settings.fps, this.settings.height)
      .then(
        (id) => {
          if (this.stream !== stream || this.disposed) {
            void bridge().preview.close(id);
            return;
          }
          stream.id = id;
          stream.opening = false;
        },
        () => {
          if (this.stream === stream) this.stream = null;
        },
      );
  }

  /** FFmpeg's picture of the frame starting at `frameStart`: it outputs the first frame at or after the requested
   * time, so ask half a millisecond early (frame times such as 1001/30000 s are not exact decimals). */
  private exactFrame(clip: TimelineClip, frameStart: number): Promise<Uint8Array | null> {
    return bridge().preview.frame(clip.path, Math.max(0, frameStart - 0.0005), this.settings.height, this.slot);
  }

  private closeStream(): void {
    const s = this.stream;
    this.stream = null;
    this.pending = false;
    if (s) this.shownT = Number.NaN; // a streamed picture can be a frame behind: fetch the exact one when paused
    if (s && s.id) void bridge().preview.close(s.id);
  }

  private async draw(jpeg: Uint8Array, t: number): Promise<void> {
    const bitmap = await createImageBitmap(new Blob([jpeg as BlobPart], { type: "image/jpeg" }));
    if (this.disposed || this.mode !== "frames") {
      bitmap.close();
      return;
    }
    const c = this.canvas;
    if (c.width !== bitmap.width || c.height !== bitmap.height) {
      c.width = bitmap.width;
      c.height = bitmap.height;
    }
    c.getContext("2d")?.drawImage(bitmap, 0, 0);
    bitmap.close();
    this.shownT = t;
    this.drawn++;
  }

  private clear(): void {
    const c = this.canvas;
    c.getContext("2d")?.clearRect(0, 0, c.width, c.height);
  }

  // ------------------------------------------------------------------ state

  private clipFps(): number {
    const rate = this.clip?.frame_rate;
    if (!rate) return 25;
    const [n, d] = rate.split("/").map(Number);
    return d ? n! / d : n || 25;
  }

  private setMode(mode: PlayerMode): void {
    if (mode === this.mode) return;
    this.mode = mode;
    this.video.style.visibility = mode === "native" ? "visible" : "hidden";
    this.canvas.style.visibility = mode === "frames" ? "visible" : "hidden";
    this.onChange?.(mode, this.fps);
  }

  private tick(): void {
    const now = performance.now();
    if (now - this.windowStart < 1000) return;
    const fps = Math.round((this.drawn * 1000) / (now - this.windowStart));
    this.drawn = 0;
    this.windowStart = now;
    if (fps !== this.fps) {
      this.fps = fps;
      this.onChange?.(this.mode, fps);
    }
  }
}
