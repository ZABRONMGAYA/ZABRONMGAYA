import { describe, expect, it } from "vitest";

import {
  MAX_PX_PER_SEC,
  anchorClip,
  clampView,
  fitView,
  frameDuration,
  hitTest,
  offsetForStart,
  timeToX,
  xToTime,
  zoomAround,
} from "../src/features/timeline/geometry";
import { clip, group } from "./fixtures";

describe("time ↔ pixels", () => {
  const view = { pxPerSec: 10, startS: -5 };

  it("round-trips", () => {
    expect(timeToX(0, view)).toBe(50);
    expect(xToTime(timeToX(123.456, view), view)).toBeCloseTo(123.456, 9);
  });

  it("zooms around a fixed time", () => {
    const zoomed = zoomAround(view, 4, 20);
    expect(zoomed.pxPerSec).toBe(40);
    expect(timeToX(20, zoomed)).toBeCloseTo(timeToX(20, view), 9);
    expect(zoomAround(view, 1e9, 0).pxPerSec).toBe(MAX_PX_PER_SEC);
  });

  it("never pans away from the footage", () => {
    // 100 px/s over 1000 px shows 10 s of a 60 s timeline.
    expect(clampView({ pxPerSec: 100, startS: -50 }, 60, 1000).startS).toBe(-9);
    expect(clampView({ pxPerSec: 100, startS: 500 }, 60, 1000).startS).toBe(59);
    expect(clampView({ pxPerSec: 100, startS: 20 }, 60, 1000).startS).toBe(20);
  });

  it("fits a group with a margin", () => {
    const fit = fitView(600, 1200);
    expect(timeToX(0, fit)).toBeGreaterThan(0);
    expect(timeToX(600, fit)).toBeLessThan(1200);
  });
});

describe("placing clips", () => {
  const recorder = clip(2, 0, 3600, { method: "reference", kind: "recorder", has_video: false });
  const camera = clip(1, 120, 600);

  it("anchors on the reference clip", () => {
    const g = group([camera, recorder]);
    expect(anchorClip(g, null)?.clip_id).toBe(2);
    expect(anchorClip(g, 1)?.clip_id).toBe(1);
  });

  it("anchors on the longest clip without a reference", () => {
    const g = group([clip(1, 0, 10), clip(2, 5, 50), clip(3, 7, 20)]);
    expect(anchorClip(g, null)?.clip_id).toBe(2);
  });

  it("turns a timeline position into an offset from the anchor", () => {
    expect(offsetForStart(130, { ...recorder, start_s: 10 })).toBe(120);
  });

  it("finds the clip under the pointer", () => {
    const g = group([camera, recorder]);
    const view = { pxPerSec: 1, startS: 0 };
    const top = (track: number) => 20 + track * 50;
    expect(hitTest(g, view, 300, 25, top, 50)?.clip.clip_id).toBe(1);
    expect(hitTest(g, view, 50, 25, top, 50)).toBeNull();
    expect(hitTest(g, view, 50, 75, top, 50)?.clip.clip_id).toBe(2);
  });

  it("uses one frame of the clip's rate", () => {
    expect(frameDuration(25)).toBe(0.04);
    expect(frameDuration(null)).toBe(0.04);
  });
});
