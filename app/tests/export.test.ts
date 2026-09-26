import { describe, expect, it } from "vitest";

import type { ExportReport } from "../src/api/contract";
import { accuracy, dominantRate } from "../src/features/export/ExportDialog";
import { clip, group } from "./fixtures";

describe("automatic export frame rate", () => {
  it("picks the video rate covering the most footage, as the engine does", () => {
    const timeline = {
      groups: [
        group([
          clip(1, 0, 3600, { has_video: false, frame_rate: null }),
          clip(2, 0, 300, { frame_rate: "24000/1001" }),
          clip(3, 10, 200, { frame_rate: "25/1" }),
          clip(4, 20, 200, { frame_rate: "25/1" }),
        ]),
      ],
      unsynced: [],
      review: [],
      reference_clip_id: 1,
      stats: { clips: 4, synced: 4, needs_review: 0, unsynced: 0 },
    };
    expect(dominantRate(timeline, 0)).toBe("25/1");
    expect(dominantRate(timeline, 3)).toBeNull();
  });
});

describe("export accuracy summary", () => {
  const report = (format: ExportReport["format"], errors: [string, number][]): ExportReport => ({
    path: "/x/Smith.xml",
    format,
    sequence: {
      name: "Smith",
      rate: "25/1",
      width: 1920,
      height: 1080,
      start_timecode: "01:00:00:00",
      duration_s: 600,
      video_tracks: 1,
      audio_tracks: 3,
    },
    clips: errors.map(([track, error_ms], i) => ({
      clip_id: i,
      name: `clip${i}`,
      track,
      start_s: 0,
      placed_s: 0,
      error_ms,
      status: "synced",
    })),
    max_error_ms: Math.max(...errors.map(([, e]) => Math.abs(e))),
    skipped: [],
    warnings: [],
  });

  it("separates cameras from recorder audio", () => {
    expect(
      accuracy(
        report("xmeml", [
          ["V1", -12.5],
          ["V1", 19.9],
          ["A3", 7.25],
        ]),
      ),
    ).toBe(
      "Cameras start on whole frames, within 19.9 ms of their synchronised positions. " +
        "Recorder audio is placed to the sample in Premiere Pro, and within 7.3 ms in DaVinci Resolve.",
    );
    expect(accuracy(report("fcpxml", [["A1", 0.01]]))).toBe("Recorder audio is placed to the sample.");
  });
});
