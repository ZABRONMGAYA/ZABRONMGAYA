import { describe, expect, it } from "vitest";

import {
  DISPLAY_LEVEL,
  PEAK_LEVELS,
  type Peaks,
  chooseLevel,
  columnPeaks,
  decodeMuLaw,
} from "../src/features/timeline/peaks";

function peaks(pairs: [number, number][], samplesPerBin = 64): Peaks {
  return {
    info: {
      directory: "/cache",
      files: {},
      encoding: "mulaw-int8-minmax",
      rate: 8000,
      samples: pairs.length * samplesPerBin,
      level_dbfs: -20,
      audio_start_s: 0,
    },
    samplesPerBin,
    data: Int8Array.from(pairs.flat()),
  };
}

describe("waveform levels", () => {
  it("chooses the coarsest level with a bin per pixel", () => {
    expect(chooseLevel(10)).toBe(64);
    expect(chooseLevel(300)).toBe(256);
    expect(chooseLevel(5000)).toBe(4096);
    expect(chooseLevel(1e9)).toBe(PEAK_LEVELS[PEAK_LEVELS.length - 1]);
  });

  it("decodes μ-law like the engine", () => {
    expect(decodeMuLaw(0)).toBe(0);
    expect(decodeMuLaw(127)).toBeCloseTo(1, 6);
    expect(decodeMuLaw(-127)).toBeCloseTo(-1, 6);
    // Reference values from mcsync.media.waveform.decode_peaks.
    expect(decodeMuLaw(64)).toBeCloseTo(0.0602084, 6);
    expect(decodeMuLaw(-100)).toBeCloseTo(-0.30490291, 6);
  });

  it("draws quiet audio visibly without saturating loud audio", () => {
    expect(DISPLAY_LEVEL[127 + 127]).toBeCloseTo(1, 6);
    expect(DISPLAY_LEVEL[0 + 127]).toBe(0);
    const quiet = DISPLAY_LEVEL[40 + 127]!;
    expect(quiet).toBeGreaterThan(0.1);
    expect(quiet).toBeLessThan(0.2);
    for (let c = -126; c <= 127; c++) expect(DISPLAY_LEVEL[c + 127]!).toBeGreaterThan(DISPLAY_LEVEL[c + 126]!);
  });
});

describe("column aggregation", () => {
  // 64 samples per bin at 8 kHz = 8 ms per bin.
  const p = peaks([
    [-10, 10],
    [-50, 20],
    [-5, 90],
    [0, 0],
  ]);

  it("takes the min and max of every bin under a column", () => {
    const out = columnPeaks(p, 2, (x) => x * 0.016);
    expect(Array.from(out)).toEqual([-50, 20, -5, 90]);
  });

  it("repeats bins when zoomed in past one bin per pixel", () => {
    const out = columnPeaks(p, 4, (x) => x * 0.004);
    expect(Array.from(out)).toEqual([-10, 10, -10, 10, -50, 20, -50, 20]);
  });

  it("leaves columns outside the audio empty", () => {
    const out = columnPeaks(p, 3, (x) => -0.016 + x * 0.016);
    expect(Array.from(out.slice(0, 2))).toEqual([0, 0]);
    expect(Array.from(out.slice(4, 6))).toEqual([-5, 90]);
  });
});
