// Draws one timeline group's clips, waveforms and edit cursor onto a single canvas.
// One canvas instead of an element per clip: a whole day of footage (hundreds of clips) stays smooth.
import type { TimelineClip, TimelineGroup } from "../../api/contract";
import { formatOffset } from "../../lib/format";
import { type View, clipSpan, timeToX, xToTime } from "./geometry";
import { DISPLAY_LEVEL, type Peaks, columnPeaks } from "./peaks";

export const TRACK_H = 58;
const PAD = 3; // between a clip and its track's edges
const LABEL_H = 16; // clip name strip above the waveform

export interface Palette {
  video: string;
  audio: string;
  wave: string;
  warn: string;
  manual: string;
  selected: string;
  outline: string;
  stripe: string;
  trackOdd: string;
  line: string;
  cursor: string;
  text: string;
  font: string;
}

export function readPalette(el: Element): Palette {
  const css = getComputedStyle(el);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    video: v("--video", "#294a70"),
    audio: v("--audio", "#2c5a46"),
    wave: v("--wave", "rgba(226,236,248,0.5)"),
    warn: v("--warn", "#f0a42c"),
    manual: v("--manual", "#a98bf7"),
    selected: "#ffffff",
    outline: "rgba(255,255,255,0.14)",
    stripe: "rgba(240,164,44,0.13)",
    trackOdd: "rgba(255,255,255,0.015)",
    line: v("--border", "#2d3239"),
    cursor: "#ff5d7a",
    text: "#ffffff",
    font: css.fontFamily,
  };
}

export interface DrawInput {
  ctx: CanvasRenderingContext2D;
  dpr: number;
  width: number;
  height: number;
  view: View;
  group: TimelineGroup;
  palette: Palette;
  selected: number | null;
  anchorId: number | null;
  drag: { clipId: number; deltaS: number } | null;
  cursorS: number | null;
  peaksFor: (clip: TimelineClip) => Peaks | null;
}

export interface DrawStats {
  clips: number;
  waveforms: number;
}

/** Where a clip is drawn, in canvas pixels (CSS units), including a drag in progress. */
export function clipBox(
  clip: TimelineClip,
  view: View,
  drag: DrawInput["drag"],
): { x0: number; x1: number; y: number; h: number; deltaS: number } | null {
  const span = clipSpan(clip);
  if (!span || clip.track === null) return null;
  const deltaS = drag?.clipId === clip.clip_id ? drag.deltaS : 0;
  return {
    x0: timeToX(span[0] + deltaS, view),
    x1: timeToX(span[1] + deltaS, view),
    y: clip.track * TRACK_H + PAD,
    h: TRACK_H - 2 * PAD,
    deltaS,
  };
}

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, radius);
}

function drawWaveform(
  ctx: CanvasRenderingContext2D,
  input: DrawInput,
  peaks: Peaks,
  clipStartS: number,
  left: number,
  right: number,
  top: number,
  height: number,
): void {
  const { dpr, view } = input;
  const c0 = Math.floor(left * dpr);
  const columns = Math.max(0, Math.ceil(right * dpr) - c0);
  if (columns === 0) return;
  const audioStart = clipStartS + peaks.info.audio_start_s;
  const minMax = columnPeaks(peaks, columns, (col) => xToTime((c0 + col) / dpr, view) - audioStart);
  const mid = (top + height / 2) * dpr;
  const scale = (height / 2) * dpr - dpr;
  for (let x = 0; x < columns; x++) {
    const lo = minMax[2 * x]!;
    const hi = minMax[2 * x + 1]!;
    if (lo === 0 && hi === 0) continue;
    const y0 = mid - DISPLAY_LEVEL[hi + 127]! * scale;
    const y1 = mid - DISPLAY_LEVEL[lo + 127]! * scale;
    ctx.fillRect(c0 + x, y0, 1, Math.max(1, y1 - y0));
  }
}

/** `text`, shortened with an ellipsis to fit `width` pixels. */
function fitText(ctx: CanvasRenderingContext2D, text: string, width: number): string {
  if (ctx.measureText(text).width <= width) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(`${text.slice(0, mid)}…`).width <= width) lo = mid;
    else hi = mid - 1;
  }
  return lo > 0 ? `${text.slice(0, lo)}…` : "";
}

function drawLabel(
  ctx: CanvasRenderingContext2D,
  input: DrawInput,
  text: string,
  x: number,
  y: number,
  maxX: number,
): number {
  if (x >= maxX - 8) return x;
  const { palette } = input;
  text = fitText(ctx, text, maxX - x);
  ctx.fillStyle = "rgba(0,0,0,0.75)";
  ctx.fillText(text, x + 1, y + 1);
  ctx.fillStyle = palette.text;
  ctx.fillText(text, x, y);
  return x + ctx.measureText(text).width;
}

function drawBadge(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxX: number,
  bg: string,
  fg: string,
): number {
  const w = ctx.measureText(text).width + 8;
  if (x + w > maxX) return x;
  ctx.fillStyle = bg;
  roundedRect(ctx, x, y - 1, w, 14, 3);
  ctx.fill();
  ctx.fillStyle = fg;
  ctx.fillText(text, x + 4, y);
  return x + w + 4;
}

export function drawTimeline(input: DrawInput): DrawStats {
  const { ctx, dpr, width, height, view, group, palette, selected, anchorId, drag, cursorS } = input;
  const stats: DrawStats = { clips: 0, waveforms: 0 };
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  for (const track of group.tracks) {
    const y = track.index * TRACK_H;
    if (track.index % 2) {
      ctx.fillStyle = palette.trackOdd;
      ctx.fillRect(0, y, width, TRACK_H);
    }
    ctx.fillStyle = palette.line;
    ctx.fillRect(0, y + TRACK_H - 1, width, 1);
  }

  ctx.font = `600 11px ${palette.font}`;
  ctx.textBaseline = "top";
  // The dragged clip is drawn last, on top.
  const clips = drag
    ? [...group.clips].sort((a, b) => Number(a.clip_id === drag.clipId) - Number(b.clip_id === drag.clipId))
    : group.clips;
  for (const clip of clips) {
    const box = clipBox(clip, view, drag);
    if (!box || box.x1 < 0 || box.x0 > width) continue;
    stats.clips++;
    // Clamp far-off edges: canvas paths with huge coordinates are slow and imprecise.
    const x0 = Math.max(box.x0, -8);
    const x1 = Math.min(box.x1, width + 8);
    const w = Math.max(1.5, x1 - x0);
    const { y, h } = box;
    const manual = clip.flags.includes("manual");
    const review = clip.status === "needs_review";

    ctx.save();
    roundedRect(ctx, x0, y, w, h, 5);
    ctx.fillStyle = clip.has_video ? palette.video : palette.audio;
    ctx.fill();
    ctx.clip();
    if (review) {
      ctx.fillStyle = palette.stripe;
      ctx.fillRect(x0, y, w, h);
    }

    const peaks = clip.has_audio && w > 3 ? input.peaksFor(clip) : null;
    if (peaks) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = palette.wave;
      drawWaveform(
        ctx,
        input,
        peaks,
        (clip.start_s ?? 0) + box.deltaS,
        Math.max(0, x0),
        Math.min(width, x1),
        y + LABEL_H,
        h - LABEL_H,
      );
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      stats.waveforms++;
    }

    if (w > 40) {
      const maxX = x1 - 6;
      let x = drawLabel(ctx, input, clip.name, Math.max(x0, 0) + 6, y + 3, maxX) + 6;
      if (clip.clip_id === anchorId) x = drawBadge(ctx, "REF", x, y + 3, maxX, "rgba(77,141,255,0.35)", "#cfe0ff");
      if (review) x = drawBadge(ctx, "review", x, y + 3, maxX, "rgba(240,164,44,0.25)", palette.warn);
      if (box.deltaS) drawBadge(ctx, formatOffset(box.deltaS), x, y + 3, maxX, "rgba(0,0,0,0.6)", palette.text);
    }
    ctx.restore();

    const isSelected = clip.clip_id === selected;
    const border = isSelected ? palette.selected : manual ? palette.manual : review ? palette.warn : palette.outline;
    const lw = isSelected || manual || review ? 2 : 1;
    ctx.lineWidth = lw;
    ctx.strokeStyle = border;
    roundedRect(ctx, x0 + lw / 2, y + lw / 2, w - lw, h - lw, 5);
    ctx.stroke();
  }

  if (cursorS !== null) {
    const x = Math.round(timeToX(cursorS, view));
    if (x >= 0 && x <= width) {
      ctx.fillStyle = palette.cursor;
      ctx.fillRect(x, 0, 1, height);
    }
  }
  return stats;
}
