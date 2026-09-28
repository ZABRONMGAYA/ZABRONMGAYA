// `window.mcsyncMulticam`: the Sync workspace's clock, audio monitor and camera windows, for end-to-end tests and
// diagnostics (read-only apart from moving the playhead).

export interface MulticamProbe {
  clock: {
    now(): number;
    play(): void;
    pause(): void;
    seek(t: number): void;
    readonly playing: boolean;
  };
  /** Sound chunks scheduled so far and the file of the last one. */
  monitor(): { chunks: number; path: string | null };
  /** How each camera window on screen decodes, and the pictures per second it shows. */
  tiles(): Record<string, { mode: string; fps: number; height: number | null }>;
}

declare global {
  interface Window {
    mcsyncMulticam?: MulticamProbe;
  }
}
