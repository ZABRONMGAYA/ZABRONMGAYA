// Timeline fixtures shared by the unit tests.
import type { TimelineClip, TimelineGroup } from "../src/api/contract";

export function clip(
  id: number,
  start: number | null,
  duration: number,
  extra: Partial<TimelineClip> = {},
): TimelineClip {
  return {
    clip_id: id,
    name: `clip${id}`,
    path: `/media/clip${id}`,
    device_id: id,
    device_name: `Device ${id}`,
    kind: "camera",
    duration_s: duration,
    has_video: true,
    has_audio: true,
    frame_rate: "25/1",
    timecode: null,
    media_status: "online",
    start_s: start,
    group: start === null ? null : 0,
    track: start === null ? null : id - 1,
    status: start === null ? "unsynced" : "synced",
    method: "audio",
    confidence: 1,
    flags: [],
    drift_ppm: 0,
    ...extra,
  };
}

export function group(clips: TimelineClip[]): TimelineGroup {
  return {
    group: 0,
    duration_s: Math.max(...clips.map((c) => (c.start_s ?? 0) + c.duration_s)),
    origin_offset_s: 0,
    tracks: clips.map((c, i) => ({
      index: i,
      device_id: c.device_id,
      device_name: c.device_name,
      kind: c.kind,
      lane: 0,
    })),
    clips,
  };
}
