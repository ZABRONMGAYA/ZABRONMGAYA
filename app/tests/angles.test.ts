import { describe, expect, it } from "vitest";

import type { TimelineClip, TimelineGroup } from "../src/api/contract";
import {
  angleAt,
  anglesOf,
  framesText,
  gridColumns,
  masterTime,
  offsetFrom,
  signedOffset,
  sourceTime,
  visibleAngles,
} from "../src/features/multicam/angles";

function clip(id: number, track: number, start: number, duration: number, extra: Partial<TimelineClip> = {}) {
  return {
    clip_id: id,
    name: `C${id}.MP4`,
    path: `/m/C${id}.MP4`,
    device_id: track,
    device_name: `Cam ${track}`,
    kind: "camera",
    duration_s: duration,
    has_video: true,
    has_audio: true,
    frame_rate: "25",
    timecode: null,
    media_status: "online",
    start_s: start,
    group: 0,
    track,
    status: "synced",
    method: "audio",
    confidence: 0.98,
    flags: [],
    drift_ppm: 0,
    ...extra,
  } as TimelineClip;
}

const group: TimelineGroup = {
  group: 0,
  duration_s: 100,
  origin_offset_s: 0,
  tracks: [
    { index: 0, device_id: 0, device_name: "A", kind: "camera", lane: 0 },
    { index: 1, device_id: 1, device_name: "B", kind: "camera", lane: 0 },
    { index: 2, device_id: 2, device_name: "Zoom", kind: "recorder", lane: 0 },
  ],
  clips: [clip(1, 0, 0, 30), clip(2, 0, 40, 30), clip(3, 1, 3.421, 50), clip(4, 2, 0, 100, { has_video: false })],
};

describe("multicam angles", () => {
  it("finds the clip each camera shows at a master time, with the offset applied", () => {
    const [a, b, zoom] = anglesOf(group);
    expect(a!.clips.map((c) => c.clip_id)).toEqual([1, 2]);
    expect(zoom!.hasVideo).toBe(false);
    const atA = angleAt(a!, 20);
    const atB = angleAt(b!, 20);
    expect(atA.kind === "media" && atA.clip.clip_id === 1 && atA.t).toBeCloseTo(20);
    expect(atB.kind === "media" && atB.t).toBeCloseTo(16.579); // 20 − 3.421: the same real moment
  });

  it("reports gaps between clips instead of the last frame", () => {
    const [a] = anglesOf(group);
    const at = angleAt(a!, 35);
    expect(at.kind).toBe("gap");
    if (at.kind === "gap") {
      expect(at.previous?.clip_id).toBe(1);
      expect(at.next?.clip_id).toBe(2);
      expect(at.nextIn).toBeCloseTo(5);
    }
    expect(angleAt(a!, 75).kind).toBe("gap");
    expect(angleAt(a!, -1).kind).toBe("gap");
  });

  it("retimes drifting clips around their middle, and inverts exactly", () => {
    const c = clip(9, 0, 10, 3600, { drift_ppm: 50 });
    expect(sourceTime(c, 10 + 1800)).toBeCloseTo(1800); // exact mid-clip
    expect(sourceTime(c, 10 + 3600) - 3600).toBeCloseTo(0.09, 3); // +50 ppm × 1800 s at the end
    expect(masterTime(c, sourceTime(c, 1234.5))).toBeCloseTo(1234.5, 6);
  });

  it("formats offsets and frames like the inspector", () => {
    expect(signedOffset(3.421)).toBe("+00:03.421");
    expect(signedOffset(-11.08)).toBe("−00:11.080");
    expect(signedOffset(3723.5)).toBe("+1:02:03.500");
    expect(framesText(3.421, 25)).toBe("+86 frames @ 25 fps");
    expect(offsetFrom(group.clips[2]!, group.clips[0]!)).toBeCloseTo(3.421);
  });

  it("lays out any number of cameras, with paging, pinning, hiding and solo", () => {
    expect([1, 2, 4, 5, 9, 12, 16].map(gridColumns)).toEqual([1, 2, 2, 3, 3, 4, 4]);
    const many = anglesOf({
      ...group,
      tracks: Array.from({ length: 20 }, (_, k) => ({
        index: k,
        device_id: k,
        device_name: `C${k}`,
        kind: "camera" as const,
        lane: 0,
      })),
      clips: Array.from({ length: 20 }, (_, k) => clip(k + 1, k, 0, 10)),
    });
    const first = visibleAngles(many, { hidden: new Set(), pinned: [], solo: null, page: 0 });
    expect(first.shown).toHaveLength(16);
    expect(first.pages).toBe(2);
    expect(visibleAngles(many, { hidden: new Set(), pinned: [], solo: null, page: 1 }).shown).toHaveLength(4);
    const pinned = visibleAngles(many, {
      hidden: new Set([many[0]!.key]),
      pinned: [many[19]!.key],
      solo: null,
      page: 0,
    });
    expect(pinned.shown[0]!.key).toBe(many[19]!.key);
    expect(pinned.shown.some((a) => a.key === many[0]!.key)).toBe(false);
    expect(visibleAngles(many, { hidden: new Set(), pinned: [], solo: many[3]!.key, page: 0 }).shown).toEqual([
      many[3],
    ]);
  });
});
