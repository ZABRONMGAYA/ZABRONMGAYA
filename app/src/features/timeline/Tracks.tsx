// The tracks of one timeline group: ruler, device tracks, and the clip canvas with its pointer gestures
// (pan, zoom, select, drag a clip to a new position). Clips are also listed, visually hidden, as buttons,
// so they can be reached with the keyboard and by screen readers.
import { type PointerEvent, memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { TimelineClip, TimelineGroup } from "../../api/contract";
import { formatTime } from "../../lib/format";
import { METHOD_LABELS, STATUS_LABELS } from "../../lib/labels";
import { useApp, usePick } from "../../state/store";
import { Ruler } from "./Ruler";
import { type DrawStats, type Palette, TRACK_H, clipBox, drawTimeline, readPalette } from "./draw";
import { anchorClip, hitTest, xToTime } from "./geometry";
import { peaksNow } from "./peaks";
import "./probe";

export const RULER_H = 26;
const DRAG_THRESHOLD_PX = 3;
const KIND_ICONS: Record<string, string> = { camera: "🎥", recorder: "🎙", phone: "📱", drone: "🛩", other: "◻︎" };

type Gesture =
  | { kind: "pan"; x0: number; startS0: number; moved: boolean }
  | { kind: "clip"; clip: TimelineClip; x0: number; moved: boolean; movable: boolean };

function clipTitle(clip: TimelineClip, isAnchor: boolean): string {
  const how = `${STATUS_LABELS[clip.status]} · ${METHOD_LABELS[clip.method]}`;
  const confidence = clip.method === "audio" ? ` · ${Math.round(clip.confidence * 100)}%` : "";
  const lines = [clip.name, `${how}${confidence}`, `Starts at ${formatTime(clip.start_s)}`];
  if (isAnchor) lines.push("Reference: other clips are placed relative to this one.");
  else lines.push("Drag to move · arrow keys nudge by a frame");
  return lines.join("\n");
}

const ClipList = memo(function ClipList({
  group,
  selected,
  anchorId,
}: {
  group: TimelineGroup;
  selected: number | null;
  anchorId: number | null;
}) {
  const select = useApp((s) => s.select);
  return (
    <ul className="sr-only" aria-label="Clips on the timeline">
      {group.clips.map((clip) => (
        <li key={clip.clip_id}>
          <button
            type="button"
            className={clip.flags.includes("manual") ? "manual" : undefined}
            aria-pressed={selected === clip.clip_id}
            onClick={() => select(clip.clip_id)}
            data-testid={`clip-${clip.name}`}
            data-status={clip.status}
            data-start={clip.start_s ?? ""}
          >
            {clip.name}
            {clip.clip_id === anchorId ? " (reference)" : ""}: {STATUS_LABELS[clip.status]}, starts at{" "}
            {formatTime(clip.start_s)}
          </button>
        </li>
      ))}
    </ul>
  );
});

export function Tracks({ group }: { group: TimelineGroup }) {
  const { view, timelineWidth, selected, cursorS, timeline } = usePick(
    "view",
    "timelineWidth",
    "selected",
    "cursorS",
    "timeline",
  );
  const peaksEpoch = useApp((s) => s.peaksEpoch);
  const { setView, setTimelineWidth, select, setCursor, moveClip, zoom } = useApp.getState();
  const area = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const hovered = useRef<number | null>(null);
  const palette = useRef<Palette | null>(null);
  const stats = useRef<DrawStats>({ clips: 0, waveforms: 0 });
  const frame = useRef(0);
  const [drag, setDrag] = useState<{ clipId: number; deltaS: number } | null>(null);
  const anchorId = anchorClip(group, timeline?.reference_clip_id ?? null)?.clip_id ?? null;
  const width = timelineWidth;
  const height = group.tracks.length * TRACK_H;

  // The latest inputs, for drawing from animation frames and for the probe.
  const latest = useRef({ view, group, width, height, selected, anchorId, drag, cursorS });
  latest.current = { view, group, width, height, selected, anchorId, drag, cursorS };

  const draw = useCallback(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    const input = latest.current;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(input.width * dpr));
    const h = Math.max(1, Math.round(input.height * dpr));
    if (el.width !== w) el.width = w; // resizing reallocates the canvas
    if (el.height !== h) el.height = h;
    palette.current ??= readPalette(el);
    stats.current = drawTimeline({
      ctx,
      dpr,
      ...input,
      palette: palette.current,
      peaksFor: (clip) => peaksNow(clip.clip_id, input.view.pxPerSec * dpr, requestDraw),
    });
  }, []); // stable: reads everything through `latest`

  // Waveforms arrive asynchronously: redraw once per frame at most.
  const requestDraw = useCallback(() => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      draw();
    });
  }, [draw]);

  // Draw in step with every change, before the browser paints.
  useLayoutEffect(draw, [draw, view, group, width, height, selected, anchorId, drag, cursorS, peaksEpoch]);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  useEffect(() => {
    window.mcsyncTimeline = {
      clipRect(name) {
        const { group, view, width } = latest.current;
        const clip = group.clips.find((c) => c.name === name);
        const box = clip && clipBox(clip, view, null);
        const rect = canvas.current?.getBoundingClientRect();
        if (!box || !rect || box.x1 < 0 || box.x0 > width) return null;
        const x0 = Math.max(0, box.x0);
        return { x: rect.left + x0, y: rect.top + box.y, width: Math.min(width, box.x1) - x0, height: box.h };
      },
      stats: () => stats.current,
    };
    return () => {
      delete window.mcsyncTimeline;
    };
  }, []);

  // Track the time area's width so zoom-to-fit and culling know it.
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setTimelineWidth(el.clientWidth));
    observer.observe(el);
    setTimelineWidth(el.clientWidth);
    return () => observer.disconnect();
  }, [setTimelineWidth]);

  // Ctrl/⌘ + wheel zooms around the pointer; horizontal wheel (or shift) pans; vertical wheel scrolls tracks.
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const { view } = useApp.getState();
      const x = e.clientX - el.getBoundingClientRect().left;
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        useApp.getState().zoom(Math.exp(-e.deltaY * 0.0025), xToTime(x, view));
        return;
      }
      const scroll = scroller.current;
      const canScroll = scroll !== null && scroll.scrollHeight > scroll.clientHeight;
      const dx = e.shiftKey
        ? e.deltaY || e.deltaX
        : Math.abs(e.deltaX) > Math.abs(e.deltaY) || !canScroll
          ? e.deltaX || e.deltaY
          : 0;
      if (dx !== 0) {
        e.preventDefault();
        useApp.getState().setView({ ...view, startS: view.startS + dx / view.pxPerSec });
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  function local(e: PointerEvent): { x: number; y: number } {
    const rect = canvas.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function clipAt(e: PointerEvent): TimelineClip | null {
    const { x, y } = local(e);
    if (y < 0) return null; // the ruler
    return hitTest(group, view, x, y, (track) => track * TRACK_H, TRACK_H)?.clip ?? null;
  }

  function onDown(e: PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    area.current!.setPointerCapture(e.pointerId);
    const clip = clipAt(e);
    const { x } = local(e);
    gesture.current = clip
      ? { kind: "clip", clip, x0: x, moved: false, movable: clip.clip_id !== anchorId }
      : { kind: "pan", x0: x, startS0: view.startS, moved: false };
  }

  function onMove(e: PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    if (!g) {
      // Hover: pointer shape and tooltip for the clip under the pointer.
      const clip = clipAt(e);
      const id = clip?.clip_id ?? null;
      if (id !== hovered.current && area.current) {
        hovered.current = id;
        area.current.style.cursor = clip ? (clip.clip_id === anchorId ? "pointer" : "grab") : "";
        area.current.title = clip ? clipTitle(clip, clip.clip_id === anchorId) : "";
      }
      return;
    }
    const dx = local(e).x - g.x0;
    if (!g.moved && Math.abs(dx) < DRAG_THRESHOLD_PX) return;
    g.moved = true;
    if (g.kind === "pan") setView({ ...view, startS: g.startS0 - dx / view.pxPerSec });
    else if (g.movable) setDrag({ clipId: g.clip.clip_id, deltaS: dx / view.pxPerSec });
  }

  async function onUp(e: PointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    gesture.current = null;
    if (!g) return;
    if (g.kind === "pan") {
      if (!g.moved) {
        setCursor(xToTime(local(e).x, view));
        select(null);
      }
      return;
    }
    select(g.clip.clip_id);
    if (!g.moved) return;
    if (!g.movable) {
      useApp.getState().toast("info", "The reference clip defines the timeline; move the other clips instead.");
      return;
    }
    const deltaS = (local(e).x - g.x0) / view.pxPerSec;
    try {
      await moveClip(g.clip.clip_id, (g.clip.start_s ?? 0) + deltaS);
    } finally {
      setDrag(null);
    }
  }

  return (
    <div className="tracks-scroll" ref={scroller}>
      <div className="tracks" style={{ height: RULER_H + height }}>
        <div className="track-headers">
          <div className="ruler-spacer" style={{ height: RULER_H }}>
            <button className="small" onClick={() => zoom(1 / 1.5)} title="Zoom out (−)">
              −
            </button>
            <button className="small" onClick={() => zoom(1.5)} title="Zoom in (+)">
              +
            </button>
          </div>
          {group.tracks.map((t) => (
            <div
              key={t.index}
              className={`track-header ${t.kind} ${t.lane > 0 ? "overflow" : ""}`}
              style={{ height: TRACK_H }}
              data-testid={`track-${t.device_name}${t.lane > 0 ? `-${t.lane}` : ""}`}
            >
              <span className="kind-icon" aria-hidden="true">
                {KIND_ICONS[t.kind] ?? KIND_ICONS.other}
              </span>
              <span className="track-name" title={t.device_name}>
                {t.device_name}
              </span>
              {t.lane > 0 && (
                <span className="badge warn" title="These clips overlap other clips of the same device">
                  overlap
                </span>
              )}
            </div>
          ))}
        </div>

        <div
          className="track-area"
          ref={area}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={(e) => void onUp(e)}
          onPointerCancel={() => {
            gesture.current = null;
            setDrag(null);
          }}
          data-testid="track-area"
        >
          <div className="ruler-row" style={{ height: RULER_H }}>
            <Ruler view={view} width={width} height={RULER_H} />
          </div>
          <canvas
            ref={canvas}
            className="clips-canvas"
            style={{ top: RULER_H, width, height }}
            data-testid="clips-canvas"
          />
          <ClipList group={group} selected={selected} anchorId={anchorId} />
        </div>
      </div>
    </div>
  );
}
