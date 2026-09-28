// The Sync inspector of the workspace (S06): the selected clip's offset from the reference, how it was found and
// on what evidence, what the preview is showing of it right now, and the manual sync tools (nudges, sync points,
// lock, reset, snap to audio, AI). Every change goes through the engine's correction log, so the preview, the
// timeline, the results and the export all follow it (and it can be undone).
import { Crosshair, Lock, RotateCcw, ScanEye, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { call } from "../../api/client";
import type { ClipMatch, TimelineClip } from "../../api/contract";
import { ConfidenceBar, SyncBadge, shortcut } from "../../design-system/components";
import { formatTime, parseRate } from "../../lib/format";
import { FLAG_LABELS, METHOD_LABELS } from "../../lib/labels";
import { useAnalyze } from "../../state/analyze";
import { usePlayback } from "../../state/playback";
import { findClip, useApp, usePick } from "../../state/store";
import { ReviewQueue } from "../inspector/ReviewQueue";
import { anchorClip, frameDuration } from "../timeline/geometry";
import { startReview } from "./ReviewBar";
import { framesText, offsetFrom, signedOffset, sourceTime } from "./angles";
import { clock, followClock } from "./clock";

/** "HIGH CONFIDENCE", "CONFIRMED", … for a placed clip (the user's statuses). */
export function statusWord(
  clip: TimelineClip,
  isReference: boolean,
): { word: string; tone: "high" | "good" | "review" | "none" } {
  if (isReference) return { word: "REFERENCE", tone: "high" };
  if (clip.status === "unsynced" || clip.start_s === null)
    return clip.flags.includes("excluded")
      ? { word: "SKIPPED", tone: "none" }
      : { word: "MANUAL SYNC REQUIRED", tone: "none" };
  if (clip.method === "manual") return { word: "CONFIRMED", tone: "high" };
  if (clip.status === "needs_review") return { word: "REVIEW RECOMMENDED", tone: "review" };
  if (clip.confidence >= 0.95)
    return (clip.corroboration ?? 0) >= 2
      ? { word: "CONFIRMED", tone: "high" }
      : { word: "HIGH CONFIDENCE", tone: "high" };
  return { word: "SYNCHRONIZED", tone: "good" };
}

/** Evidence in a line: agreeing windows, how far the peak stands out, how many devices agree. */
export function evidenceLine(clip: TimelineClip, matches: ClipMatch[]): string {
  const best = matches.find((m) => !m.rejected && m.status !== "no_match");
  const parts: string[] = [];
  if (clip.method === "audio" && best) {
    if (best.inliers !== undefined && best.windows) parts.push(`${best.inliers}/${best.windows} windows agree`);
    if (best.prominence) parts.push(`peak ${best.prominence.toFixed(0)}× noise`);
    else if (best.correlation) parts.push(`correlation ${best.correlation.toFixed(2)}`);
  }
  const agreeing = clip.corroboration ?? 0;
  if (agreeing) parts.push(`${agreeing} other source${agreeing === 1 ? "" : "s"} agree`);
  if (clip.method === "metadata") parts.push("camera clock only (±1 s)");
  if (clip.method === "timecode") parts.push("timecode");
  if (clip.method === "manual") parts.push("placed by you");
  if (clip.flags.includes("clock_mismatch")) parts.push("a weak audio match contradicted the camera clock");
  return parts.join(" · ") || "—";
}

function driftText(clip: TimelineClip): string {
  if (Math.abs(clip.drift_ppm) < 0.05) return "None measured";
  const fps = parseRate(clip.frame_rate) ?? 25;
  const perHour = clip.drift_ppm * 1e-6 * 3600 * fps;
  return `${perHour >= 0 ? "+" : "−"}${Math.abs(perHour).toFixed(1)} fr / hr (${clip.drift_ppm.toFixed(1)} ppm) · corrected`;
}

interface Nudge {
  label: string;
  keys: string;
  run(clip: TimelineClip, frame: number): void;
  playhead?: boolean;
}

const NUDGES: Nudge[] = [
  { label: "◀ 1 fr", keys: "←", playhead: true, run: (c) => clock.step(-1, parseRate(c.frame_rate) ?? 25) },
  { label: "1 fr ▶", keys: "→", playhead: true, run: (c) => clock.step(1, parseRate(c.frame_rate) ?? 25) },
  { label: "−1 ms", keys: "⌥ ,", run: (c) => useApp.getState().nudge(c.clip_id, -0.001) },
  { label: "+1 ms", keys: "⌥ .", run: (c) => useApp.getState().nudge(c.clip_id, 0.001) },
  { label: "−1 fr", keys: ",", run: (c, f) => useApp.getState().nudge(c.clip_id, -f) },
  { label: "+1 fr", keys: ".", run: (c, f) => useApp.getState().nudge(c.clip_id, f) },
  { label: "−10 fr", keys: "⇧ ,", run: (c, f) => useApp.getState().nudge(c.clip_id, -10 * f) },
  { label: "+10 fr", keys: "⇧ .", run: (c, f) => useApp.getState().nudge(c.clip_id, 10 * f) },
];

/** Set a sync point on the selected clip at the playhead (S). */
export async function addSyncPoint(clip: TimelineClip): Promise<void> {
  const T = clock.now();
  const app = useApp.getState();
  if (clip.start_s === null || T < clip.start_s || T > clip.start_s + clip.duration_s) {
    app.toast("info", "Move the playhead over the clip first: a sync point pins the moment under the playhead.");
    return;
  }
  const points = await app.run(() =>
    call("sync.add_point", { clip_id: clip.clip_id, source_s: sourceTime(clip, T), group_s: T }),
  );
  if (points) usePlayback.getState().setPoints({ ...points, clipId: clip.clip_id });
}

export function SyncInspector() {
  const { timeline, selected, matches, group, cursorS } = usePick(
    "timeline",
    "selected",
    "matches",
    "group",
    "cursorS",
  );
  const points = usePlayback((s) => s.points);
  const stats = usePlayback((s) => s.stats);
  const active = usePlayback((s) => s.active);
  const [showEvidence, setShowEvidence] = useState(false);
  const master = useRef<HTMLSpanElement>(null);
  const source = useRef<HTMLSpanElement>(null);
  const clip = findClip(timeline, selected);

  // Sync points of the selected clip.
  useEffect(() => {
    if (selected === null) return;
    let live = true;
    void call("sync.points", { clip_id: selected }).then(
      (p) => live && usePlayback.getState().setPoints({ ...p, clipId: selected }),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [selected, timeline]);

  // Master and source time under the playhead, every frame while playing.
  useEffect(
    () =>
      followClock((T) => {
        if (master.current) master.current.textContent = formatTime(T);
        if (source.current) {
          const c = findClip(useApp.getState().timeline, useApp.getState().selected);
          const inside = c && c.start_s !== null && T >= c.start_s && T <= c.start_s + c.duration_s;
          source.current.textContent = inside ? formatTime(sourceTime(c, T)) : "outside the clip";
        }
      }),
    [],
  );

  if (selected === null || !clip) {
    return (
      <aside className="sy-si" data-testid="inspector">
        <div className="sy-si__head">
          <span className="sy-eyebrow">Sync inspector</span>
        </div>
        <p className="sy-si__hint sy-dim">Click a camera or a clip to see how it was synchronised and to adjust it.</p>
        {(timeline?.review.length ?? 0) > 0 && (
          <div className="sy-si__more">
            <button
              type="button"
              className="sy-si__act sy-si__act--primary"
              onClick={startReview}
              data-testid="start-review"
            >
              <span>
                Review {timeline!.review.length.toLocaleString()} clip{timeline!.review.length === 1 ? "" : "s"}
              </span>
            </button>
          </div>
        )}
        <ReviewQueue />
      </aside>
    );
  }

  const g = timeline?.groups.find((x) => x.group === (clip.group ?? group));
  const anchor = g && anchorClip(g, timeline!.reference_clip_id);
  const isAnchor = anchor?.clip_id === clip.clip_id;
  const isReference = timeline?.reference_clip_id === clip.clip_id || clip.method === "reference";
  const placed = clip.start_s !== null;
  const fps = parseRate(clip.frame_rate) ?? 25;
  const frame = frameDuration(parseRate(clip.frame_rate));
  const offset = anchor ? offsetFrom(clip, anchor) : null;
  const status = statusWord(clip, isReference || isAnchor);
  const clipMatches = matches[clip.clip_id] ?? [];
  const matchesLoaded = matches[clip.clip_id] !== undefined;
  const excluded = clip.flags.includes("excluded");
  const manual = clip.flags.includes("manual") || clip.method === "manual";
  const tile = active ? stats[active] : undefined;
  const previewText = !tile
    ? "Not on screen"
    : tile.mode === "native"
      ? `Playing natively · ${tile.fps} fps`
      : tile.mode === "frames"
        ? `FFmpeg ${tile.height ?? ""}p · ${tile.fps} fps`
        : "No picture at the playhead";

  return (
    <aside className="sy-si" data-testid="inspector">
      <div className="sy-si__head">
        <span className="sy-eyebrow">Sync inspector</span>
        <button
          type="button"
          className="sy-si__close"
          aria-label="Close"
          onClick={() => useApp.getState().select(null)}
        >
          <X size={14} aria-hidden />
        </button>
      </div>

      <div className="sy-si__offset">
        <div className="sy-si__row">
          <span className="sy-si__camera" title={clip.name}>
            {clip.device_name.toUpperCase()}
          </span>
          <span className={`sy-si__status sy-si__status--${status.tone}`} data-testid="inspector-status">
            {status.word}
          </span>
        </div>
        <span className="sy-si__big tnum" data-testid="inspector-offset">
          {isAnchor ? "REF" : offset !== null ? signedOffset(offset) : "—"}
        </span>
        <span className="sy-si__sub tnum">
          {isAnchor
            ? "The timeline's reference: other clips are placed relative to it"
            : offset !== null
              ? `= ${framesText(offset, fps)} · relative to ${anchor?.device_name ?? "the reference"}`
              : "Not placed on the timeline yet"}
        </span>
      </div>

      <div className="sy-si__facts">
        <span>Clip</span>
        <span className="sy-ellipsis" title={clip.path}>
          {clip.name}
        </span>
        <span>Method</span>
        <span>{METHOD_LABELS[clip.method]}</span>
        <span>Confidence</span>
        <div className="sy-si__conf">
          <strong>{clip.method === "manual" || isReference ? "—" : `${Math.round(clip.confidence * 100)}%`}</strong>
          {clip.method !== "manual" && !isReference && (
            <ConfidenceBar value={clip.confidence} tone={status.tone === "none" ? "review" : status.tone} />
          )}
        </div>
        <span>Evidence</span>
        <span>
          {evidenceLine(clip, clipMatches)}{" "}
          <button
            type="button"
            className="sy-link"
            onClick={() => setShowEvidence(!showEvidence)}
            data-testid="show-evidence"
          >
            {showEvidence ? "Hide" : "Show evidence"}
          </button>
        </span>
        <span>Drift</span>
        <span>{driftText(clip)}</span>
        <span>Preview</span>
        <span data-testid="inspector-preview">{previewText}</span>
        <span>Master time</span>
        <span className="tnum" ref={master} />
        <span>Source time</span>
        <span className="tnum" ref={source} />
        {clip.flags.length > 0 && (
          <>
            <span>Notes</span>
            <span className="sy-si__flags">{clip.flags.map((f) => FLAG_LABELS[f] ?? f).join(" · ")}</span>
          </>
        )}
      </div>

      {showEvidence &&
        (matchesLoaded || clip.method === "ai" ? (
          <Evidence clip={clip} matches={clipMatches} />
        ) : (
          <div className="sy-si__evidence sy-dim">Loading the matches…</div>
        ))}

      {!isAnchor && (
        <>
          <div className="sy-si__section sy-eyebrow">Manual sync</div>
          <div className="sy-si__nudges" role="group" aria-label="Nudge">
            {NUDGES.map((n) => (
              <button
                key={n.label}
                type="button"
                disabled={!placed && !n.playhead}
                onClick={() => n.run(clip, frame)}
                title={n.playhead ? "Move the playhead" : "Move the clip"}
              >
                <strong>{n.label}</strong>
                <span>{n.keys}</span>
              </button>
            ))}
          </div>
          <div className="sy-si__actions">
            <button
              type="button"
              className="sy-si__act"
              onClick={() => void addSyncPoint(clip)}
              disabled={!placed}
              data-testid="set-sync-point"
            >
              <Crosshair size={14} aria-hidden />
              <span>Set sync point</span>
              <kbd>S</kbd>
            </button>
            <button
              type="button"
              className="sy-si__act sy-si__act--primary"
              onClick={() => void useApp.getState().confirm(clip.clip_id)}
              disabled={!placed}
              title="Accept this position: the clip is locked here (confirmed)"
              data-testid="confirm"
            >
              <Lock size={14} aria-hidden />
              <span>Lock sync</span>
              <kbd>{shortcut("⌘L")}</kbd>
            </button>
            <button
              type="button"
              className="sy-si__act sy-si__act--ghost"
              onClick={() => void useApp.getState().correct("clear_offset", clip.clip_id)}
              disabled={!manual}
              title="Forget the manual placement: back to what the engine found"
              data-testid="reset"
            >
              <RotateCcw size={14} aria-hidden />
              <span>Reset sync</span>
              <kbd>{shortcut("⌘⌫")}</kbd>
            </button>
            <button
              type="button"
              className="sy-si__act sy-si__act--ai"
              onClick={() => void useAnalyze.getState().openAiSync(clip.clip_id)}
              title="Look for the clip's place from what was said, light changes and the camera clocks"
              data-testid="find-with-ai"
            >
              <ScanEye size={14} aria-hidden />
              <span>Re-run AI</span>
              <kbd>{shortcut("⇧A")}</kbd>
            </button>
          </div>
          <div className="sy-si__more">
            {placed && clip.has_audio && anchor?.has_audio && (
              <button
                type="button"
                className="sy-link"
                onClick={() => void useApp.getState().snap(clip.clip_id)}
                data-testid="snap"
              >
                Snap to audio
              </button>
            )}
            <button
              type="button"
              className="sy-link"
              onClick={() => void useApp.getState().placeAtCursor(clip.clip_id)}
              disabled={cursorS === null}
              title={
                cursorS === null
                  ? "Click the timeline to set the playhead first"
                  : `Start the clip at ${formatTime(cursorS)}`
              }
              data-testid="place-at-cursor"
            >
              Place at playhead
            </button>
            {excluded ? (
              <button
                type="button"
                className="sy-link"
                onClick={() => void useApp.getState().correct("include", clip.clip_id)}
              >
                Include in sync
              </button>
            ) : (
              <button
                type="button"
                className="sy-link"
                onClick={() => void useApp.getState().correct("exclude", clip.clip_id)}
                disabled={isReference}
                data-testid="exclude"
              >
                Exclude
              </button>
            )}
            {clip.has_audio && !isReference && (
              <button
                type="button"
                className="sy-link"
                onClick={() => void useApp.getState().setReference(clip.clip_id)}
                data-testid="set-reference"
              >
                Use as reference
              </button>
            )}
          </div>
        </>
      )}

      {points && points.clipId === clip.clip_id && points.points.length > 0 && (
        <div className="sy-si__points" data-testid="sync-points">
          <div className="sy-si__section sy-eyebrow">Sync points</div>
          <ul>
            {points.points.map((p) => (
              <li key={p.id} className="tnum">
                <span>{formatTime(p.source_s)}</span>
                <span className="sy-dim">→ {formatTime(p.group_s)}</span>
                <span>{anchor ? signedOffset(p.offset_s - (anchor.start_s ?? 0)) : ""}</span>
                <button
                  type="button"
                  className="sy-si__close"
                  aria-label="Remove sync point"
                  onClick={() =>
                    void useApp
                      .getState()
                      .run(() => call("sync.remove_point", { point_id: p.id }))
                      .then((r) => r && usePlayback.getState().setPoints({ ...r, clipId: clip.clip_id }))
                  }
                >
                  <X size={12} aria-hidden />
                </button>
              </li>
            ))}
          </ul>
          <p className="sy-dim sy-si__pointsum">
            {points.points.length === 1
              ? "One point: add another further along the clip to measure its drift."
              : points.agree
                ? `${points.points.length} points agree within half a frame · drift ${points.drift_ppm?.toFixed(1)} ppm`
                : points.agree === false
                  ? "The points disagree by more than half a frame: check them, or the clip's clock is not steady."
                  : "The points are too close together to measure drift."}
          </p>
          {points.points.length >= 1 && (
            <button
              type="button"
              className="sy-link"
              onClick={() => {
                const avg = points.points.reduce((n, p) => n + p.offset_s, 0) / points.points.length;
                void useApp.getState().moveClip(clip.clip_id, avg);
              }}
              data-testid="apply-points"
            >
              Place the clip by its sync points
            </button>
          )}
        </div>
      )}
    </aside>
  );
}

function Evidence({ clip, matches }: { clip: TimelineClip; matches: ClipMatch[] }) {
  const correct = useApp((s) => s.correct);
  if (clip.method === "ai" || clip.flags.includes("ai_proposal")) {
    return (
      <div className="sy-si__evidence" data-testid="evidence">
        <p className="sy-dim">
          This position comes from AI sync: speech, light changes and the camera clocks, not from matching audio.
        </p>
        <button type="button" className="sy-link" onClick={() => void useAnalyze.getState().openAiSync(clip.clip_id)}>
          Open the AI evidence
        </button>
      </div>
    );
  }
  if (matches.length === 0)
    return (
      <div className="sy-si__evidence" data-testid="evidence">
        <p className="sy-dim">
          No audio match involves this clip{clip.method === "metadata" ? ": it is placed by its camera clock" : ""}.
        </p>
      </div>
    );
  return (
    <div className="sy-si__evidence" data-testid="matches">
      <ul>
        {matches.map((m) => (
          <li
            key={m.other_clip_id}
            className={m.rejected ? "rejected" : undefined}
            title={m.flags.map((f) => FLAG_LABELS[f] ?? f).join("\n")}
          >
            <span className="sy-ellipsis">
              {m.other_name}
              {m.other_device ? <span className="sy-dim"> · {m.other_device}</span> : null}
            </span>
            <span className="tnum">{m.offset_s === null ? "—" : signedOffset(m.offset_s)}</span>
            <span className="tnum">{Math.round(m.confidence * 100)}%</span>
            <span className="sy-dim tnum">
              {m.inliers ?? "?"}/{m.windows ?? "?"} win{m.prominence ? ` · ${m.prominence.toFixed(0)}×` : ""}
            </span>
            {m.rejected ? (
              <button
                type="button"
                className="sy-link"
                onClick={() => void correct("unreject_pair", clip.clip_id, m.other_clip_id)}
              >
                Restore
              </button>
            ) : (
              <button
                type="button"
                className="sy-link"
                onClick={() => void correct("reject_pair", clip.clip_id, m.other_clip_id)}
                data-testid={`reject-${m.other_name}`}
              >
                Reject
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
