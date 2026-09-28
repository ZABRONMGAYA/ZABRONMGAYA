// The multicamera viewer of the Sync workspace (S06): every camera of the sync group playing at the master clock's
// time, with its offsets applied. Grid (paged beyond 16 cameras: only the cameras on screen load media), program
// (one camera large), and compare (review: the reference on the left, the clip under review on the right). One
// source is heard at a time (the audio monitor); the others are muted.
import { ChevronLeft, ChevronRight, Columns2, Grid2x2, Maximize, Square, Volume2, VolumeX } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { PreviewCaps, TimelineClip, TimelineGroup } from "../../api/contract";
import { bridge } from "../../api/client";
import { PREVIEW_HEIGHTS, type Quality, usePlayback } from "../../state/playback";
import { useApp } from "../../state/store";
import { anchorClip } from "../timeline/geometry";
import { AngleTile, TileTools } from "./AngleTile";
import { type Angle, angleAt, anglesOf, gridColumns, masterTime, visibleAngles } from "./angles";
import { AudioMonitor, type MonitorResolver } from "./audio";
import { clock } from "./clock";

let monitor: AudioMonitor | null = null;
/** The app's one audio monitor (created on first use: an AudioContext needs a user gesture to start). */
export function audioMonitor(): AudioMonitor {
  monitor ??= new AudioMonitor();
  return monitor;
}

/** What the monitor plays: the clips of one camera, in step with the master clock. */
export function resolverFor(clips: TimelineClip[]): MonitorResolver {
  const angle: Angle = {
    key: "monitor",
    track: { index: 0, device_id: null, device_name: "", kind: "recorder", lane: 0 },
    clips: clips.filter((c) => c.has_audio && c.start_s !== null && c.media_status === "online"),
    hasVideo: false,
    hasAudio: true,
  };
  return (t) => {
    const at = angleAt(angle, t);
    if (at.kind === "gap") return { nextAt: at.next?.start_s ?? null };
    const c = at.clip;
    return {
      source: {
        path: c.path,
        stream: null,
        t: at.t,
        clipEnd: masterTime(c, c.duration_s),
        rate: 1 + c.drift_ppm * 1e-6,
      },
    };
  };
}

/** The camera heard when the user has not chosen one: the reference recorder, else the first with sound. */
export function autoMonitor(angles: Angle[], reference: TimelineClip | undefined): Angle | undefined {
  const withSound = angles.filter((a) => a.hasAudio);
  return (
    withSound.find((a) => a.track.kind === "recorder" && a.clips.some((c) => c.clip_id === reference?.clip_id)) ??
    withSound.find((a) => a.track.kind === "recorder") ??
    withSound.find((a) => a.clips.some((c) => c.clip_id === reference?.clip_id)) ??
    withSound[0]
  );
}

/** For review: the confidently placed clip of another camera that overlaps `clip` the longest (video first). */
export function comparePartner(group: TimelineGroup, clip: TimelineClip): TimelineClip | undefined {
  if (clip.start_s === null) return anchorClip(group, null);
  const a0 = clip.start_s;
  const a1 = clip.start_s + clip.duration_s;
  let best: { c: TimelineClip; score: number } | undefined;
  for (const c of group.clips) {
    if (c.clip_id === clip.clip_id || c.start_s === null || c.device_id === clip.device_id) continue;
    if (c.status !== "synced") continue;
    const overlap = Math.min(a1, c.start_s + c.duration_s) - Math.max(a0, c.start_s);
    if (overlap <= 0) continue;
    const score = overlap + (c.has_video ? 1e6 : 0);
    if (!best || score > best.score) best = { c, score };
  }
  return best?.c;
}

const NONE: TimelineClip[] = [];

const QUALITY_OPTIONS: { value: string; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "performance", label: "Performance (360p)" },
  { value: "quality", label: "Quality (original)" },
  ...PREVIEW_HEIGHTS.map((h) => ({ value: `custom:${h}`, label: `${h}p` })),
  { value: "custom:0", label: "Original" },
];

export function MulticamViewer({ group }: { group: TimelineGroup }) {
  const referenceId = useApp((s) => s.timeline?.reference_clip_id ?? null);
  const selected = useApp((s) => s.selected);
  const unsynced = useApp((s) => s.timeline?.unsynced ?? NONE);
  const pb = usePlayback();
  const root = useRef<HTMLDivElement>(null);
  const [caps, setCaps] = useState<PreviewCaps | null>(null);

  const angles = useMemo(() => anglesOf(group), [group]);
  const reference = useMemo(() => anchorClip(group, referenceId), [group, referenceId]);
  const { shown, pages } = visibleAngles(angles, {
    hidden: pb.hidden,
    pinned: pb.pinned,
    solo: pb.solo,
    page: pb.page,
  });
  const indexOf = useMemo(() => new Map(angles.map((a, k) => [a.key, k])), [angles]);

  useEffect(() => {
    void bridge()
      .preview.caps()
      .then(setCaps, () => undefined);
  }, []);

  // The monitored source follows the choice (auto: the reference recorder).
  const heard =
    pb.monitor === "mute"
      ? undefined
      : pb.monitor
        ? angles.find((a) => a.key === pb.monitor)
        : autoMonitor(angles, reference);
  const heardKey = heard?.key ?? null;
  useEffect(() => {
    const m = audioMonitor();
    const angle = angles.find((a) => a.key === heardKey);
    m.setSource(angle ? resolverFor(angle.clips) : null);
  }, [heardKey, angles]);

  // The active camera follows the selected clip.
  useEffect(() => {
    if (selected === null) return;
    const angle = angles.find((a) => a.clips.some((c) => c.clip_id === selected));
    if (angle && angle.key !== usePlayback.getState().active) usePlayback.getState().setActive(angle.key);
  }, [selected, angles]);

  function selectAngle(angle: Angle, clip: TimelineClip | null) {
    pb.setActive(angle.key);
    const pick = clip ?? angle.clips[0] ?? null;
    if (pick) useApp.getState().select(pick.clip_id);
  }

  function openProgram(angle: Angle) {
    pb.setActive(angle.key);
    pb.setProgram(angle.key);
    pb.setLayout(pb.layout === "program" && pb.program === angle.key ? "grid" : "program");
  }

  const tools = (a: Angle) => (
    <TileTools
      heard={heardKey === a.key}
      solo={pb.solo === a.key}
      pinned={pb.pinned.includes(a.key)}
      onHear={() => pb.setMonitor(a.key)}
      onSolo={() => pb.setSolo(pb.solo === a.key ? null : a.key)}
      onHide={() => pb.toggleHidden(a.key)}
      onPin={() => pb.togglePinned(a.key)}
    />
  );

  const tile = (a: Angle, extra: { only?: TimelineClip | null; label?: string; count: number }) => (
    <AngleTile
      key={`${a.key}:${extra.only?.clip_id ?? "all"}`}
      angle={a}
      index={indexOf.get(a.key) ?? 0}
      only={extra.only}
      label={extra.label}
      reference={reference}
      active={pb.active === a.key}
      program={pb.program === a.key}
      monitored={heardKey === a.key}
      pinned={pb.pinned.includes(a.key)}
      tiles={extra.count}
      onSelect={selectAngle}
      onOpen={openProgram}
      tools={extra.only === undefined ? tools(a) : undefined}
    />
  );

  let body;
  if (pb.layout === "compare" && selected !== null) {
    // The clip may not be placed yet (manual sync required): it is shown on its own, marked NOT YET SYNCED.
    const clip = group.clips.find((c) => c.clip_id === selected) ?? unsynced.find((c) => c.clip_id === selected);
    const candidateAngle =
      angles.find((a) => a.clips.some((c) => c.clip_id === selected)) ??
      (clip
        ? {
            key: `clip:${clip.clip_id}`,
            track: { index: -1, device_id: clip.device_id, device_name: clip.device_name, kind: clip.kind, lane: 0 },
            clips: [clip],
            hasVideo: clip.has_video,
            hasAudio: clip.has_audio,
          }
        : undefined);
    const partner = clip ? comparePartner(group, clip) : undefined;
    const partnerAngle = partner ? angles.find((a) => a.clips.some((c) => c.clip_id === partner.clip_id)) : undefined;
    body = (
      <div className="mc-grid mc-grid--compare" data-testid="viewer-compare">
        {partnerAngle ? (
          tile(partnerAngle, { label: "REFERENCE", count: 2 })
        ) : (
          <div className="mc-empty">No synced camera overlaps this clip</div>
        )}
        {candidateAngle && clip ? (
          tile(candidateAngle, { only: clip, label: "UNDER REVIEW", count: 2 })
        ) : (
          <div className="mc-empty">Not on this timeline</div>
        )}
      </div>
    );
  } else if (pb.layout === "program") {
    const prog = angles.find((a) => a.key === pb.program) ?? shown[0];
    const others = shown.filter((a) => a !== prog).slice(0, 4);
    body = (
      <div className="mc-grid mc-grid--program" data-testid="viewer-program">
        {prog && tile(prog, { count: 1 })}
        {others.length > 0 && <div className="mc-strip">{others.map((a) => tile(a, { count: 8 }))}</div>}
      </div>
    );
  } else {
    const cols = gridColumns(shown.length);
    body = (
      <div
        className="mc-grid"
        style={{
          gridTemplateColumns: `repeat(${cols}, 1fr)`,
          gridTemplateRows: `repeat(${Math.ceil(shown.length / cols) || 1}, 1fr)`,
        }}
        data-testid="viewer-grid"
      >
        {shown.map((a) => tile(a, { count: shown.length }))}
        {shown.length === 0 && <div className="mc-empty">No camera to show{pb.hidden.size ? " (all hidden)" : ""}</div>}
      </div>
    );
  }

  const stats = Object.entries(pb.stats).filter(([k]) => shown.some((a) => a.key === k));
  const native = stats.filter(([, s]) => s.mode === "native").length;
  const frames = stats.filter(([, s]) => s.mode === "frames").length;
  const fps = stats.length ? Math.round(stats.reduce((n, [, s]) => n + s.fps, 0) / stats.length) : 0;
  const qualityValue = pb.quality === "custom" ? `custom:${pb.customHeight}` : pb.quality;
  const audioAngles = angles.filter((a) => a.hasAudio);

  return (
    <section className="mc-viewer" ref={root} data-testid="multicam-viewer" onClick={() => pb.setActive(null)}>
      {body}
      <div className="mc-bar" onClick={(e) => e.stopPropagation()}>
        <div className="mc-seg" role="group" aria-label="Layout">
          <button
            type="button"
            aria-pressed={pb.layout === "grid"}
            title="All cameras"
            onClick={() => pb.setLayout("grid")}
            data-testid="layout-grid"
          >
            <Grid2x2 size={13} aria-hidden />
          </button>
          <button
            type="button"
            aria-pressed={pb.layout === "program"}
            title="Program: one camera large (Enter)"
            onClick={() => {
              if (!pb.program && pb.active) pb.setProgram(pb.active);
              pb.setLayout("program");
            }}
            data-testid="layout-program"
          >
            <Square size={13} aria-hidden />
          </button>
          <button
            type="button"
            aria-pressed={pb.layout === "compare"}
            title="Compare the selected clip with the reference"
            disabled={selected === null}
            onClick={() => pb.setLayout(pb.layout === "compare" ? "grid" : "compare")}
            data-testid="layout-compare"
          >
            <Columns2 size={13} aria-hidden />
          </button>
        </div>
        {pages > 1 && pb.layout === "grid" && (
          <span className="mc-pages">
            <button
              type="button"
              aria-label="Previous cameras"
              onClick={() => pb.setPage(Math.max(0, pb.page - 1))}
              disabled={pb.page === 0}
            >
              <ChevronLeft size={13} aria-hidden />
            </button>
            {Math.min(pb.page, pages - 1) + 1} / {pages}
            <button
              type="button"
              aria-label="More cameras"
              onClick={() => pb.setPage(Math.min(pages - 1, pb.page + 1))}
              disabled={pb.page >= pages - 1}
            >
              <ChevronRight size={13} aria-hidden />
            </button>
          </span>
        )}
        {(pb.hidden.size > 0 || pb.solo) && (
          <button type="button" className="mc-link" onClick={() => pb.showAll()}>
            Show all{pb.hidden.size ? ` (${pb.hidden.size} hidden)` : ""}
          </button>
        )}
        <span className="mc-bar__grow" />
        <label className="mc-select" title="Which camera is heard (one at a time)">
          {pb.monitor === "mute" ? <VolumeX size={13} aria-hidden /> : <Volume2 size={13} aria-hidden />}
          <select
            value={pb.monitor ?? "auto"}
            onChange={(e) => pb.setMonitor(e.target.value === "auto" ? null : e.target.value)}
            data-testid="monitor-select"
          >
            <option value="auto">Auto{heard && pb.monitor === null ? ` · ${heard.track.device_name}` : ""}</option>
            <option value="mute">Muted</option>
            {audioAngles.map((a) => (
              <option key={a.key} value={a.key}>
                {a.track.device_name}
                {a.track.lane ? ` ${a.track.lane + 1}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="mc-select" title="Preview quality: pictures only, the media is never changed">
          <select
            value={qualityValue}
            onChange={(e) => {
              const [q, h] = e.target.value.split(":");
              pb.setQuality(q as Quality, h !== undefined ? Number(h) : undefined);
            }}
            data-testid="quality-select"
          >
            {QUALITY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <span
          className="mc-stats tnum"
          data-testid="preview-stats"
          title={
            caps
              ? `FFmpeg hardware decoders: ${caps.hwaccels.join(", ") || "none"} · browser video decode: ${caps.videoDecode}`
              : undefined
          }
        >
          {fps} fps · {native ? `${native} native` : ""}
          {native && frames ? " · " : ""}
          {frames ? `${frames} FFmpeg` : ""}
          {caps && native ? ` · browser ${caps.videoDecode.startsWith("enabled") ? "GPU" : "CPU"} decode` : ""}
          {caps && frames ? ` · FFmpeg ${caps.hwaccels.length ? "GPU when available" : "CPU"}` : ""}
        </span>
        <button
          type="button"
          title="Full screen (F)"
          aria-label="Full screen"
          onClick={() =>
            void (document.fullscreenElement ? document.exitFullscreen() : root.current?.requestFullscreen())
          }
        >
          <Maximize size={13} aria-hidden />
        </button>
      </div>
    </section>
  );
}

/** Keep the master clock's end at the group's end, and the clock inside it when the group changes. */
export function useGroupClock(group: TimelineGroup | undefined): void {
  useEffect(() => {
    if (!group) return;
    clock.end = group.duration_s;
    if (clock.now() > group.duration_s) clock.seek(0);
  }, [group]);
}
