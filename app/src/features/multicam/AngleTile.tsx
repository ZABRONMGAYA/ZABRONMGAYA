// One camera window of the multicamera viewer (S06): the clip this camera recorded at the master clock's time,
// its name, source timecode, offset from the reference and sync status. When the camera was not recording then,
// the window says so (NO MEDIA, with the time to its next clip) rather than holding a last frame; a clip whose file
// is missing says OFFLINE; a clip that still needs review keeps playing under a REVIEW REQUIRED badge.
import { Eye, EyeOff, Headphones, Pin, PinOff, Star, TriangleAlert, Unplug, VideoOff } from "lucide-react";
import { type MouseEvent, type ReactNode, memo, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { TimelineClip } from "../../api/contract";
import { SyncBadge } from "../../design-system/components";
import { formatTimecode, parseRate } from "../../lib/format";
import { previewHeight, usePlayback } from "../../state/playback";
import { type Angle, angleAt, offsetFrom, signedOffset } from "./angles";
import { clock, followClock } from "./clock";
import { TilePlayer } from "./player";

type View =
  | { kind: "media"; clipId: number }
  | { kind: "gap"; nextIn: number | null; outside: boolean }
  | { kind: "offline"; clipId: number }
  | { kind: "unsynced" };

function sameView(a: View, b: View): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "gap" && b.kind === "gap") return a.nextIn === b.nextIn && a.outside === b.outside;
  if ((a.kind === "media" || a.kind === "offline") && (b.kind === "media" || b.kind === "offline"))
    return a.clipId === b.clipId;
  return true;
}

/** "CAM A", "CAM B", … "CAM AA" for the 27th. */
export function angleLetter(index: number): string {
  let s = "";
  let n = index;
  do {
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return s;
}

function clipTimecode(clip: TimelineClip, t: number): string {
  const rate = parseRate(clip.frame_rate) ?? 25;
  let base = 0;
  if (clip.timecode) {
    const m = /^(\d+):(\d+):(\d+)[:;.](\d+)$/.exec(clip.timecode);
    if (m) base = +m[1]! * 3600 + +m[2]! * 60 + +m[3]! + +m[4]! / Math.round(rate);
  }
  return formatTimecode(base + Math.max(0, t), rate);
}

export interface TileProps {
  angle: Angle;
  index: number;
  /** The clip shown alone (review compare): other clips of the camera are "outside clip". */
  only?: TimelineClip | null;
  reference: TimelineClip | undefined;
  active: boolean;
  program: boolean;
  monitored: boolean;
  pinned: boolean;
  tiles: number;
  onSelect(angle: Angle, clip: TimelineClip | null): void;
  onOpen(angle: Angle): void;
  label?: string;
  /** Per-camera controls, shown on hover. */
  tools?: ReactNode;
}

export const AngleTile = memo(function AngleTile(props: TileProps) {
  const { angle, index, only, reference, active, program, monitored, pinned, tiles, label } = props;
  const box = useRef<HTMLDivElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const tc = useRef<HTMLSpanElement>(null);
  const player = useRef<TilePlayer | null>(null);
  const [view, setView] = useState<View>({ kind: "gap", nextIn: null, outside: true });
  const [boxHeight, setBoxHeight] = useState(0);
  const quality = usePlayback((s) => s.quality);
  const customHeight = usePlayback((s) => s.customHeight);

  const clips = only ? [only] : angle.clips;
  const latest = useRef({ clips, only });
  latest.current = { clips, only };

  // The player, created once per window.
  useLayoutEffect(() => {
    const p = new TilePlayer(video.current!, canvas.current!, `tile:${angle.key}`);
    p.onChange = (mode, fps) =>
      usePlayback.getState().report(angle.key, { mode, fps, height: mode === "frames" ? p.settings.height : null });
    player.current = p;
    return () => {
      p.dispose();
      player.current = null;
    };
  }, [angle.key]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setBoxHeight(el.clientHeight));
    observer.observe(el);
    setBoxHeight(el.clientHeight);
    return () => observer.disconnect();
  }, []);

  // Quality → how this window decodes: natively (original) where it can, else FFmpeg pictures sized to the window.
  useEffect(() => {
    const p = player.current;
    if (!p || boxHeight === 0) return;
    const height = previewHeight(quality, customHeight, boxHeight, program);
    const preferNative =
      quality === "quality" || (quality === "auto" && tiles <= 4) || (quality === "custom" && customHeight === 0);
    p.configure({ preferNative, height, fps: quality === "performance" && !program ? 15 : 25 }, clock.now());
  }, [quality, customHeight, boxHeight, program, tiles]);

  // Follow the master clock: the right clip at the right source time, every frame while playing.
  useEffect(() => {
    const at = (T: number): { clip: TimelineClip | null; view: View; t: number } => {
      const { clips, only } = latest.current;
      if (only && only.start_s === null) return { clip: only, view: { kind: "unsynced" }, t: 0 };
      const found = angleAt({ ...angle, clips }, T);
      if (found.kind === "gap") {
        const nextIn = found.nextIn === null ? null : Math.ceil(found.nextIn);
        return { clip: null, view: { kind: "gap", nextIn, outside: only !== undefined && only !== null }, t: 0 };
      }
      if (found.clip.media_status !== "online")
        return { clip: null, view: { kind: "offline", clipId: found.clip.clip_id }, t: found.t };
      return { clip: found.clip, view: { kind: "media", clipId: found.clip.clip_id }, t: found.t };
    };
    let current: View = { kind: "gap", nextIn: null, outside: true };
    return followClock((T, playing) => {
      const r = at(T);
      if (r.view.kind === "unsynced") {
        // Not placed yet: its first picture, so the camera can be recognised.
        player.current?.update(r.clip, 0, false, clock.rate);
      } else {
        player.current?.update(r.clip, r.t, playing && !clock.scrubbing, clock.rate);
      }
      if (!sameView(current, r.view)) {
        current = r.view;
        setView(r.view);
      }
      if (tc.current) tc.current.textContent = r.clip ? clipTimecode(r.clip, r.t) : "--:--:--:--";
    });
  }, [angle]);

  const shownClip =
    view.kind === "media" || view.kind === "offline" ? clips.find((c) => c.clip_id === view.clipId) : (only ?? null);
  const offset = shownClip && reference ? offsetFrom(shownClip, reference) : null;
  const isReference = shownClip !== null && shownClip !== undefined && reference?.clip_id === shownClip.clip_id;
  const review = shownClip?.status === "needs_review";
  const letter = angleLetter(index);

  function onClick(e: MouseEvent) {
    e.stopPropagation();
    const T = clock.now();
    const found = angleAt(angle, T);
    props.onSelect(angle, found.kind === "media" ? found.clip : (shownClip ?? null));
  }

  return (
    <div
      ref={box}
      className={`mc-tile ${active ? "mc-tile--active" : ""} ${review ? "mc-tile--review" : ""}`}
      onClick={onClick}
      onDoubleClick={(e) => {
        e.stopPropagation();
        props.onOpen(angle);
      }}
      role="button"
      tabIndex={-1}
      aria-pressed={active}
      aria-label={`${label ?? `Camera ${letter}`} · ${angle.track.device_name}`}
      data-testid={`angle-${angle.track.device_name}`}
      data-view={view.kind}
    >
      <video ref={video} className="mc-tile__video" muted playsInline disablePictureInPicture />
      <canvas ref={canvas} className="mc-tile__canvas" />

      {view.kind !== "media" && (
        <div className="mc-tile__state" data-testid="tile-state">
          {view.kind === "gap" && (
            <>
              <VideoOff size={20} aria-hidden />
              <strong>{view.outside ? "OUTSIDE CLIP" : "NO MEDIA"}</strong>
              <span>
                {view.outside
                  ? "The clip under review does not cover this moment"
                  : view.nextIn !== null
                    ? `Not recording · next clip in ${signedOffset(view.nextIn).slice(1, -4)}`
                    : "Not recording"}
              </span>
            </>
          )}
          {view.kind === "offline" && (
            <>
              <Unplug size={20} aria-hidden />
              <strong>OFFLINE</strong>
              <span>The file is not where it was imported from</span>
            </>
          )}
          {view.kind === "unsynced" && (
            <>
              <TriangleAlert size={20} aria-hidden />
              <strong>NOT YET SYNCED</strong>
              <span>Place it at the cursor, snap it to audio, or find it with AI</span>
            </>
          )}
        </div>
      )}

      <div className="mc-tile__top">
        <span className="mc-chip mc-chip--label">
          {label ?? `CAM ${letter}`} · {angle.track.device_name}
        </span>
        <span className="mc-tile__icons">
          {program && <span className="mc-chip mc-chip--program">PROGRAM</span>}
          {monitored && <Headphones size={12} aria-label="Heard" />}
          {pinned && <Pin size={12} aria-label="Pinned" />}
          {shownClip &&
            (isReference ? (
              <SyncBadge status="reference" label="REFERENCE" />
            ) : review ? (
              <SyncBadge
                status="review"
                label="REVIEW REQUIRED"
                confidence={shownClip.method === "audio" ? shownClip.confidence : null}
              />
            ) : shownClip.status === "unsynced" ? (
              <SyncBadge status="none" label="NOT YET SYNCED" />
            ) : (
              <SyncBadge
                status={shownClip.confidence >= 0.95 || shownClip.method === "manual" ? "high" : "good"}
                label={
                  shownClip.method === "manual"
                    ? "CONFIRMED"
                    : (shownClip.corroboration ?? 0) >= 2
                      ? "CONFIRMED"
                      : undefined
                }
                confidence={shownClip.method === "manual" ? null : shownClip.confidence}
              />
            ))}
        </span>
      </div>
      {props.tools}
      <div className="mc-tile__bottom tnum">
        <span className="mc-chip" title={shownClip?.path}>
          {shownClip ? shownClip.name : "—"} · <span ref={tc}>--:--:--:--</span>
        </span>
        <span className="mc-chip">{isReference ? "REF" : offset !== null ? signedOffset(offset) : "—"}</span>
      </div>
    </div>
  );
});

/** Per-camera controls on hover: hear, solo, hide, pin (the viewer passes what to do). */
export function TileTools(props: {
  heard: boolean;
  solo: boolean;
  pinned: boolean;
  onHear(): void;
  onSolo(): void;
  onHide(): void;
  onPin(): void;
}) {
  return (
    <div className="mc-tile__tools" onClick={(e) => e.stopPropagation()}>
      <button type="button" title="Hear this camera (A)" aria-pressed={props.heard} onClick={props.onHear}>
        <Headphones size={13} aria-hidden />
      </button>
      <button type="button" title="Solo: show this camera alone" aria-pressed={props.solo} onClick={props.onSolo}>
        <Star size={13} aria-hidden />
      </button>
      <button type="button" title="Hide this camera" onClick={props.onHide}>
        {props.solo ? <Eye size={13} aria-hidden /> : <EyeOff size={13} aria-hidden />}
      </button>
      <button type="button" title={props.pinned ? "Unpin" : "Pin to the first page"} onClick={props.onPin}>
        {props.pinned ? <PinOff size={13} aria-hidden /> : <Pin size={13} aria-hidden />}
      </button>
    </div>
  );
}
