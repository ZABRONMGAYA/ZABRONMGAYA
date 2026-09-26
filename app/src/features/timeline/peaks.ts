// Waveform overviews: which zoom level to read, loading from the engine cache, per-pixel aggregation.
import { bridge, call } from "../../api/client";
import type { WaveformInfo } from "../../api/contract";

export const PEAK_LEVELS = [64, 256, 1024, 4096, 16384, 65536] as const;

/** The coarsest level that still has at least one bin per pixel. */
export function chooseLevel(samplesPerPixel: number): number {
  let level: number = PEAK_LEVELS[0];
  for (const l of PEAK_LEVELS) if (l <= samplesPerPixel) level = l;
  return level;
}

export interface Peaks {
  info: WaveformInfo;
  samplesPerBin: number;
  /** Interleaved (min, max) μ-law pairs. */
  data: Int8Array;
}

const infos = new Map<number, Promise<WaveformInfo | null>>();
const levels = new Map<string, Promise<Peaks | null>>();
// Settled results, for drawing synchronously inside an animation frame.
const readyInfos = new Map<number, WaveformInfo | null>();
const readyLevels = new Map<string, Peaks>();
let generation = 0;

export function forgetPeaks(): void {
  infos.clear();
  levels.clear();
  readyInfos.clear();
  readyLevels.clear();
  generation++;
}

/**
 * The best waveform available right now for drawing a clip at `pxPerColumn` pixels per second: the level
 * matching the zoom if it is loaded, otherwise the nearest loaded level (or null). Missing data is requested,
 * and `onLoaded` runs when it arrives so the caller can redraw.
 */
export function peaksNow(clipId: number, pxPerColumn: number, onLoaded: () => void): Peaks | null {
  const info = readyInfos.get(clipId);
  if (info === undefined) {
    if (!infos.has(clipId)) {
      const gen = generation;
      void waveformInfo(clipId).then((i) => {
        if (gen !== generation) return;
        readyInfos.set(clipId, i);
        onLoaded();
      });
    }
    return null;
  }
  if (info === null) return null;
  const level = chooseLevel(info.rate / pxPerColumn);
  const exact = readyLevels.get(`${clipId}:${level}`);
  if (exact) return exact;
  if (!levels.has(`${clipId}:${level}`)) {
    const gen = generation;
    void loadPeaks(clipId, level).then((p) => {
      if (gen !== generation || !p) return;
      readyLevels.set(`${clipId}:${level}`, p);
      onLoaded();
    });
  }
  // Nearest loaded level: search outwards from the wanted one (runs per clip per frame, so no allocation).
  const at = PEAK_LEVELS.indexOf(level as (typeof PEAK_LEVELS)[number]);
  for (let d = 1; d < PEAK_LEVELS.length; d++) {
    for (const i of [at - d, at + d]) {
      const l = PEAK_LEVELS[i];
      const p = l === undefined ? undefined : readyLevels.get(`${clipId}:${l}`);
      if (p) return p;
    }
  }
  return null;
}

export function waveformInfo(clipId: number): Promise<WaveformInfo | null> {
  let p = infos.get(clipId);
  if (!p) {
    p = call("waveform.info", { clip_id: clipId }).catch(() => null);
    infos.set(clipId, p);
  }
  return p;
}

export function loadPeaks(clipId: number, samplesPerBin: number): Promise<Peaks | null> {
  const key = `${clipId}:${samplesPerBin}`;
  let p = levels.get(key);
  if (!p) {
    p = (async () => {
      const info = await waveformInfo(clipId);
      if (!info) return null;
      const bins = Math.ceil(info.samples / samplesPerBin);
      const file = info.files[String(samplesPerBin)];
      if (!file || bins === 0) return null;
      const bytes = await bridge().readPeaks(info.directory, file, 0, bins * 2);
      return { info, samplesPerBin, data: new Int8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength) };
    })().catch(() => null);
    levels.set(key, p);
  }
  return p;
}

const MU = 255;

/** μ-law code (−127…127) → linear amplitude (−1…1). */
export function decodeMuLaw(code: number): number {
  const y = code / 127;
  return (Math.sign(y) * Math.expm1(Math.abs(y) * Math.log1p(MU))) / MU;
}

/**
 * Drawn height (−1…1) for a μ-law code: the square root of the amplitude, so quiet camera audio stays
 * visible without busy audio filling the whole clip (μ-law alone is too compressed to read).
 */
export const DISPLAY_LEVEL: Float32Array = (() => {
  const table = new Float32Array(255);
  for (let code = -127; code <= 127; code++) {
    const a = decodeMuLaw(code);
    table[code + 127] = Math.sign(a) * Math.sqrt(Math.abs(a));
  }
  return table;
})();

/**
 * Min/max (μ-law units, −127…127) of the audio under each pixel column.
 * `timeAt(x)` maps a column's left edge to seconds from the clip's first audio sample.
 */
export function columnPeaks(peaks: Peaks, columns: number, timeAt: (x: number) => number): Int8Array {
  const out = new Int8Array(columns * 2);
  const { data, samplesPerBin, info } = peaks;
  const nBins = data.length / 2;
  const binRate = info.rate / samplesPerBin;
  for (let x = 0; x < columns; x++) {
    const b0 = Math.floor(timeAt(x) * binRate);
    const b1 = Math.max(b0 + 1, Math.floor(timeAt(x + 1) * binRate));
    let lo = 127;
    let hi = -127;
    for (let b = Math.max(0, b0); b < Math.min(nBins, b1); b++) {
      const mn = data[2 * b]!;
      const mx = data[2 * b + 1]!;
      if (mn < lo) lo = mn;
      if (mx > hi) hi = mx;
    }
    if (hi >= lo) {
      out[2 * x] = lo;
      out[2 * x + 1] = hi;
    }
  }
  return out;
}
