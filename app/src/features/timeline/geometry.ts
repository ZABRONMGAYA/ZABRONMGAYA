// Pure timeline geometry: time ↔ pixels, zoom, and placing clips relative to the reference.
import type { TimelineClip, TimelineGroup } from "../../api/contract";

export interface View {
  /** Horizontal scale. */
  pxPerSec: number;
  /** Timeline time at the left edge of the time area. */
  startS: number;
}

export const MIN_PX_PER_SEC = 0.02; // a whole day across ~1700 px
export const MAX_PX_PER_SEC = 20_000; // individual audio samples at 8 kHz

export const timeToX = (t: number, v: View): number => (t - v.startS) * v.pxPerSec;
export const xToTime = (x: number, v: View): number => x / v.pxPerSec + v.startS;

export function clampPxPerSec(px: number): number {
  return Math.min(MAX_PX_PER_SEC, Math.max(MIN_PX_PER_SEC, px));
}

/** Zoom by `factor`, keeping timeline time `anchorS` under the same pixel. */
export function zoomAround(v: View, factor: number, anchorS: number): View {
  const pxPerSec = clampPxPerSec(v.pxPerSec * factor);
  const anchorX = timeToX(anchorS, v);
  return { pxPerSec, startS: anchorS - anchorX / pxPerSec };
}

/** Keep at least a tenth of the view on the timeline `[0, durationS]`, so panning never loses the footage. */
export function clampView(v: View, durationS: number, widthPx: number): View {
  const visible = widthPx / v.pxPerSec;
  const min = -0.9 * visible;
  const max = durationS - 0.1 * visible;
  return { pxPerSec: v.pxPerSec, startS: Math.min(max, Math.max(min, v.startS)) };
}

/** Show `[0, durationS]` in `widthPx`, with a small margin. */
export function fitView(durationS: number, widthPx: number): View {
  const margin = 0.03 * durationS + 1;
  const pxPerSec = clampPxPerSec(widthPx / Math.max(durationS + 2 * margin, 1));
  return { pxPerSec, startS: -margin };
}

export function clipSpan(clip: TimelineClip): [number, number] | null {
  return clip.start_s === null ? null : [clip.start_s, clip.start_s + clip.duration_s];
}

/** Timeline position of the reference clip in a group (the anchor manual offsets are relative to). */
export function anchorClip(group: TimelineGroup, referenceId: number | null): TimelineClip | undefined {
  return (
    group.clips.find((c) => c.clip_id === referenceId) ??
    group.clips.find((c) => c.method === "reference") ??
    [...group.clips].sort((a, b) => b.duration_s - a.duration_s)[0]
  );
}

/** The manual offset (relative to the anchor) that puts a clip at timeline time `startS`. */
export function offsetForStart(startS: number, anchor: TimelineClip): number {
  return startS - (anchor.start_s ?? 0);
}

/** One frame at the clip's rate (25 fps for audio-only clips). */
export function frameDuration(rate: number | null): number {
  return 1 / (rate ?? 25);
}

export interface ClipHit {
  clip: TimelineClip;
  track: number;
}

/** The clip under a point, given track geometry. */
export function hitTest(
  group: TimelineGroup,
  v: View,
  x: number,
  y: number,
  trackTop: (track: number) => number,
  trackHeight: number,
): ClipHit | null {
  const t = xToTime(x, v);
  for (const clip of group.clips) {
    const span = clipSpan(clip);
    if (!span || clip.track === null) continue;
    const top = trackTop(clip.track);
    if (y >= top && y < top + trackHeight && t >= span[0] && t <= span[1]) return { clip, track: clip.track };
  }
  return null;
}
