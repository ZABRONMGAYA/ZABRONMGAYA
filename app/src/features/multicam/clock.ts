// The master timeline clock: the one authority every camera player, the audio monitor, the timeline playhead,
// the waveforms and the timecode read their position from. Players never keep their own clocks: each frame they
// compare their media position with `now()` and correct themselves.

export type ClockEvent = "play" | "pause" | "seek" | "rate";

export class MasterClock {
  private anchorT = 0; // master time at `anchorPerf`
  private anchorPerf = 0;
  private listeners = new Set<(event: ClockEvent) => void>();
  playing = false;
  rate = 1;
  /** True while the user drags the playhead (players show pictures, not playback). */
  scrubbing = false;
  /** Where the timeline ends (playback stops there). */
  end = Number.POSITIVE_INFINITY;

  constructor(private readonly perf: () => number = () => performance.now()) {}

  now(): number {
    if (!this.playing) return this.anchorT;
    const t = this.anchorT + ((this.perf() - this.anchorPerf) / 1000) * this.rate;
    if (t >= this.end) {
      this.anchorT = this.end;
      this.playing = false;
      this.emit("pause");
      return this.end;
    }
    return t;
  }

  play(): void {
    if (this.playing) return;
    this.anchorT = this.now();
    this.anchorPerf = this.perf();
    this.playing = true;
    this.emit("play");
  }

  pause(): void {
    if (!this.playing) return;
    this.anchorT = this.now();
    this.playing = false;
    this.emit("pause");
  }

  toggle(): void {
    if (this.playing) this.pause();
    else this.play();
  }

  seek(t: number): void {
    this.anchorT = Math.max(0, Math.min(t, this.end));
    this.anchorPerf = this.perf();
    this.emit("seek");
  }

  /** Step by whole frames (negative: back). Pauses first, as an editor does. */
  step(frames: number, fps: number): void {
    this.pause();
    const frame = 1 / (fps || 25);
    this.seek(Math.round(this.now() / frame) * frame + frames * frame);
  }

  setRate(rate: number): void {
    this.anchorT = this.now();
    this.anchorPerf = this.perf();
    this.rate = rate;
    this.emit("rate");
  }

  subscribe(fn: (event: ClockEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  private emit(event: ClockEvent): void {
    for (const fn of this.listeners) fn(event);
  }
}

/** The app's one master clock (group time of the timeline on screen). */
export const clock = new MasterClock();

/**
 * Call `fn(t)` on every animation frame while the clock plays, and once after each seek or pause: the cheapest
 * way for many views to follow one clock without re-rendering React.
 */
export function followClock(fn: (t: number, playing: boolean) => void): () => void {
  let frame = 0;
  const tick = () => {
    frame = 0;
    fn(clock.now(), clock.playing);
    if (clock.playing) frame = requestAnimationFrame(tick);
  };
  const off = clock.subscribe(() => {
    if (!frame) frame = requestAnimationFrame(tick);
  });
  frame = requestAnimationFrame(tick);
  return () => {
    off();
    if (frame) cancelAnimationFrame(frame);
  };
}
