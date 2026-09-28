// The multicamera preview's model: which camera (angle) shows which clip at a master time, and where in that clip.
// Pure functions of the timeline, shared by the viewer, the inspector and the tests.
import type { TimelineClip, TimelineGroup, TimelineTrack } from "../../api/contract";

export interface Angle {
  /** Stable key of the angle: its device (and lane, when a device's clips overlap). */
  key: string;
  track: TimelineTrack;
  /** Placed clips of the angle, by start. */
  clips: TimelineClip[];
  hasVideo: boolean;
  hasAudio: boolean;
}

/** One angle per track of the group, in track order (clips without a place are not in a timeline group). */
export function anglesOf(group: TimelineGroup): Angle[] {
  const byTrack = new Map<number, TimelineClip[]>();
  for (const c of group.clips) {
    if (c.start_s === null || c.track === null) continue;
    const list = byTrack.get(c.track) ?? [];
    list.push(c);
    byTrack.set(c.track, list);
  }
  return group.tracks.map((track) => {
    const clips = (byTrack.get(track.index) ?? []).sort((a, b) => a.start_s! - b.start_s!);
    return {
      key: `${track.device_id ?? "x"}:${track.lane}:${track.index}`,
      track,
      clips,
      hasVideo: clips.some((c) => c.has_video),
      hasAudio: clips.some((c) => c.has_audio),
    };
  });
}

/**
 * Where in a clip a master time falls. The solver aligns a drifting clip at its middle (its reported start is
 * exact there, ±drift·duration/2 at the ends); the preview retimes it exactly: source time runs at (1 + drift)
 * times the master clock around the clip's middle.
 */
export function sourceTime(clip: TimelineClip, masterT: number): number {
  const mid = clip.duration_s / 2;
  return mid + (masterT - (clip.start_s ?? 0) - mid) * (1 + clip.drift_ppm * 1e-6);
}

/** The master time at which a clip shows source time `t` (the inverse of `sourceTime`). */
export function masterTime(clip: TimelineClip, t: number): number {
  const mid = clip.duration_s / 2;
  return (clip.start_s ?? 0) + mid + (t - mid) / (1 + clip.drift_ppm * 1e-6);
}

export type AngleAt =
  | { kind: "media"; clip: TimelineClip; t: number }
  | { kind: "gap"; previous: TimelineClip | null; next: TimelineClip | null; nextIn: number | null };

/** The clip of `angle` covering master time `masterT`, or the gap it falls in (a camera not recording then). */
export function angleAt(angle: Angle, masterT: number): AngleAt {
  const clips = angle.clips;
  let lo = 0;
  let hi = clips.length - 1;
  let last = -1; // the last clip starting at or before masterT
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (clips[mid]!.start_s! <= masterT) {
      last = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  // Overlapping clips of one device sit on other lanes; within a lane the latest-starting covering clip wins.
  for (let k = last; k >= 0 && k >= last - 3; k--) {
    const c = clips[k]!;
    if (masterT < c.start_s! + c.duration_s) return { kind: "media", clip: c, t: sourceTime(c, masterT) };
  }
  const next = clips[last + 1] ?? null;
  return {
    kind: "gap",
    previous: last >= 0 ? clips[last]! : null,
    next,
    nextIn: next ? next.start_s! - masterT : null,
  };
}

/** Offset of a clip relative to the reference clip (its start minus the reference's start). */
export function offsetFrom(clip: TimelineClip, reference: TimelineClip | undefined): number | null {
  if (clip.start_s === null || !reference || reference.start_s === null) return null;
  return clip.start_s - reference.start_s;
}

/** "+00:03.421" (hours from an hour on). */
export function signedOffset(seconds: number): string {
  const sign = seconds < 0 ? "−" : "+";
  let ms = Math.round(Math.abs(seconds) * 1000);
  const h = Math.floor(ms / 3_600_000);
  ms -= h * 3_600_000;
  const m = Math.floor(ms / 60_000);
  ms -= m * 60_000;
  const s = Math.floor(ms / 1000);
  ms -= s * 1000;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${sign}${h ? `${h}:` : ""}${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}

/** "= +85 frames @ 25 fps" (rounded to whole frames). */
export function framesText(seconds: number, fps: number): string {
  const frames = Math.round(seconds * fps);
  return `${frames >= 0 ? "+" : "−"}${Math.abs(frames).toLocaleString()} frames @ ${Number(fps.toFixed(3))} fps`;
}

/** Grid columns for `n` visible angles (2 × 2 up to 4, 3 × 3 up to 9, 4 × 4 up to 16). */
export function gridColumns(n: number): number {
  if (n <= 1) return 1;
  if (n <= 4) return 2;
  if (n <= 9) return 3;
  return 4;
}

export const PAGE_SIZE = 16;

/** The angles on screen: pinned first, hidden ones out, solo alone; `page` of PAGE_SIZE. */
export function visibleAngles(
  angles: Angle[],
  opts: { hidden: Set<string>; pinned: string[]; solo: string | null; page: number; videoOnly?: boolean },
): { shown: Angle[]; pages: number } {
  let list = angles.filter((a) => (opts.videoOnly === false || a.hasVideo) && !opts.hidden.has(a.key));
  if (opts.solo) {
    const solo = angles.find((a) => a.key === opts.solo);
    return { shown: solo ? [solo] : [], pages: 1 };
  }
  const pinned = opts.pinned.map((k) => list.find((a) => a.key === k)).filter((a): a is Angle => a !== undefined);
  list = [...pinned, ...list.filter((a) => !opts.pinned.includes(a.key))];
  const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
  const page = Math.min(Math.max(0, opts.page), pages - 1);
  return { shown: list.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), pages };
}
