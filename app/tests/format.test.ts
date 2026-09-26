import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatOffset,
  formatTime,
  formatTimecode,
  parseRate,
  rateLabel,
  rulerStep,
} from "../src/lib/format";

describe("frame rates", () => {
  it("parses rational rates", () => {
    expect(parseRate("30000/1001")).toBeCloseTo(29.97, 2);
    expect(parseRate("25")).toBe(25);
    expect(parseRate(null)).toBeNull();
    expect(parseRate("0/0")).toBeNull();
  });

  it("labels rates the way editors say them", () => {
    expect(rateLabel("24000/1001")).toBe("23.976");
    expect(rateLabel("30000/1001")).toBe("29.97");
    expect(rateLabel("50/1")).toBe("50");
    expect(rateLabel(null)).toBe("audio");
  });
});

describe("times", () => {
  it("formats positions with and without hours", () => {
    expect(formatTime(0)).toBe("00:00.000");
    expect(formatTime(75.5)).toBe("01:15.500");
    expect(formatTime(3 * 3600 + 62.25, 2)).toBe("3:01:02.25");
    expect(formatTime(-1.5, 1)).toBe("-00:01.5");
    expect(formatTime(null)).toBe("—");
  });

  it("rounds without producing 60 seconds", () => {
    expect(formatTime(59.9996)).toBe("01:00.000");
  });

  it("formats timecode-style labels", () => {
    expect(formatTimecode(3661.5, 25)).toBe("01:01:01:12");
  });

  it("formats durations and offsets", () => {
    expect(formatDuration(7500)).toBe("2 h 05 min");
    expect(formatDuration(200)).toBe("3 min 20 s");
    expect(formatDuration(4.25)).toBe("4.3 s");
    expect(formatOffset(0.0000123)).toBe("+12 µs");
    expect(formatOffset(-0.0421)).toBe("−42.1 ms");
    expect(formatOffset(120.3)).toBe("+120.300 s");
  });

  it("picks ruler steps that keep labels apart", () => {
    expect(rulerStep(1)).toBe(120);
    expect(rulerStep(100)).toBe(1);
    expect(rulerStep(10_000)).toBe(0.01);
    for (const px of [0.02, 0.5, 3, 40, 900]) expect(rulerStep(px) * px).toBeGreaterThanOrEqual(80 - 1e-9);
  });
});
