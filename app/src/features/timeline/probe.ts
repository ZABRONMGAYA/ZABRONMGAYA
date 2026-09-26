// `window.mcsyncTimeline`: read-only timeline geometry for end-to-end tests and diagnostics.
import type { DrawStats } from "./draw";

export interface TimelineProbe {
  /** Where the canvas draws a clip, in page coordinates; null when it is off screen. */
  clipRect(name: string): { x: number; y: number; width: number; height: number } | null;
  /** What the last frame drew. */
  stats(): DrawStats;
}

declare global {
  interface Window {
    mcsyncTimeline?: TimelineProbe;
  }
}
