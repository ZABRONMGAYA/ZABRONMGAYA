// Details of the selected clip: how it was placed, why it needs review, and the tools to correct it.
import type { ClipMatch, ClipSummary, TimelineClip } from "../../api/contract";
import { formatDuration, formatOffset, formatTime, parseRate, rateLabel } from "../../lib/format";
import { FLAG_LABELS, METHOD_LABELS, STATUS_LABELS, driftSummary } from "../../lib/labels";
import { useAnalyze } from "../../state/analyze";
import { findClip, useApp, usePick } from "../../state/store";
import { anchorClip, frameDuration } from "../timeline/geometry";

function Facts({ clip, summary }: { clip: TimelineClip | undefined; summary: ClipSummary | undefined }) {
  const rows: [string, string][] = [];
  const device = clip?.device_name ?? summary?.device_name;
  if (device) rows.push(["Device", device]);
  if (clip?.start_s !== null && clip?.start_s !== undefined) rows.push(["Starts at", formatTime(clip.start_s)]);
  rows.push(["Duration", formatDuration(clip?.duration_s ?? summary?.duration_s ?? 0)]);
  rows.push(["Frame rate", rateLabel(clip?.frame_rate ?? summary?.frame_rate)]);
  const timecode = clip?.timecode ?? summary?.timecode;
  if (timecode) rows.push(["Timecode", timecode]);
  if (summary?.creation_time) rows.push(["Recorded", new Date(summary.creation_time).toLocaleString()]);
  if (summary?.chapter) rows.push(["Chapter", `${summary.chapter.index + 1} of take ${summary.chapter.take}`]);
  return (
    <dl className="facts">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
      <div>
        <dt>File</dt>
        <dd className="path" title={clip?.path ?? summary?.path}>
          {clip?.path ?? summary?.path}
        </dd>
      </div>
    </dl>
  );
}

function MatchRow({ clipId, match }: { clipId: number; match: ClipMatch }) {
  const correct = useApp((s) => s.correct);
  const flags = match.flags.map((f) => FLAG_LABELS[f] ?? f).join("\n");
  return (
    <li className={`match ${match.status} ${match.rejected ? "rejected" : ""}`} title={flags || undefined}>
      <span className="other">{match.other_name}</span>
      <span className="offset">{match.offset_s === null ? "—" : formatOffset(match.offset_s)}</span>
      <span className="confidence">{Math.round(match.confidence * 100)}%</span>
      <span className="actions">
        {match.rejected ? (
          <button className="small" onClick={() => void correct("unreject_pair", clipId, match.other_clip_id)}>
            Restore
          </button>
        ) : (
          <>
            {match.offset_s !== null && (
              <button
                className="small"
                title="Place the clip exactly where this match puts it"
                onClick={() => void correct("offset", clipId, match.other_clip_id, match.offset_s!)}
              >
                Use
              </button>
            )}
            <button
              className="small"
              title="This match is wrong: ignore it and solve again"
              onClick={() => void correct("reject_pair", clipId, match.other_clip_id)}
              data-testid={`reject-${match.other_name}`}
            >
              Reject
            </button>
          </>
        )}
      </span>
    </li>
  );
}

export function Inspector() {
  const { timeline, media, selected, matches, snaps, group, cursorS } = usePick(
    "timeline",
    "media",
    "selected",
    "matches",
    "snaps",
    "group",
    "cursorS",
  );
  const { nudge, snap, confirm, placeAtCursor, correct, setReference } = useApp.getState();

  const clip = findClip(timeline, selected);
  const summary = media.clips.find((c) => c.clip_id === selected);
  if (selected === null || (!clip && !summary)) {
    return (
      <section className="inspector" data-testid="inspector">
        <p className="muted">Select a clip to see how it was placed and to correct it.</p>
      </section>
    );
  }

  const name = clip?.name ?? summary!.name;
  const g = timeline?.groups.find((x) => x.group === (clip?.group ?? group));
  const anchor = g && anchorClip(g, timeline!.reference_clip_id);
  const isAnchor = anchor?.clip_id === selected;
  const placed = clip?.start_s !== null && clip?.start_s !== undefined;
  const hasAudio = clip?.has_audio ?? summary?.audio_stream !== null;
  const flags = clip?.flags ?? [];
  const manual = flags.includes("manual");
  const excluded = flags.includes("excluded");
  const isReference = timeline?.reference_clip_id === selected;
  const frame = frameDuration(parseRate(clip?.frame_rate ?? summary?.frame_rate ?? null));
  const drift = clip ? driftSummary(clip) : null;
  const clipMatches = matches[selected] ?? [];
  // Candidates no analysis window confirmed are noise, not choices.
  const alternatives = (snaps[selected]?.alternatives ?? []).filter((alt) => alt.n_inliers >= 2);
  const currentOffset = placed && anchor ? clip!.start_s! - (anchor.start_s ?? 0) : null;

  return (
    <section className="inspector" data-testid="inspector">
      <h2 title={name}>{name}</h2>
      {clip && (
        <div className="status-line">
          <span className={`pill ${clip.status}`} data-testid="inspector-status">
            {STATUS_LABELS[clip.status]}
          </span>
          <span>{METHOD_LABELS[clip.method]}</span>
          {(clip.method === "audio" || clip.method === "ai") && (
            <span className="muted">{Math.round(clip.confidence * 100)}% confident</span>
          )}
          {isAnchor && <span className="badge ref">REF</span>}
        </div>
      )}

      <Facts clip={clip} summary={summary} />

      {(flags.length > 0 || drift) && (
        <ul className="flags">
          {flags.map((f) => (
            <li key={f} className={f}>
              {FLAG_LABELS[f] ?? f}
            </li>
          ))}
          {drift && <li className="drift">{drift}</li>}
        </ul>
      )}

      {timeline && (
        <div className="actions">
          {placed && !isAnchor && (
            <div className="nudge" role="group" aria-label="Nudge">
              <span className="muted">Nudge</span>
              <button className="small" onClick={() => void nudge(selected, -10 * frame)} title="10 frames earlier">
                −10f
              </button>
              <button className="small" onClick={() => void nudge(selected, -frame)} title="1 frame earlier (←)">
                −1f
              </button>
              <button className="small" onClick={() => void nudge(selected, -0.001)} title="1 ms earlier (Alt+←)">
                −1ms
              </button>
              <button className="small" onClick={() => void nudge(selected, 0.001)} title="1 ms later (Alt+→)">
                +1ms
              </button>
              <button className="small" onClick={() => void nudge(selected, frame)} title="1 frame later (→)">
                +1f
              </button>
              <button className="small" onClick={() => void nudge(selected, 10 * frame)} title="10 frames later">
                +10f
              </button>
            </div>
          )}
          <div className="buttons">
            {placed && !isAnchor && hasAudio && anchor?.has_audio && (
              <button
                onClick={() => void snap(selected)}
                title="Search the audio within ±2 s of the current position"
                data-testid="snap"
              >
                Snap to audio
              </button>
            )}
            {placed && !isAnchor && clip?.status === "needs_review" && (
              <button
                onClick={() => void confirm(selected)}
                title="Lock the clip where it is now"
                data-testid="confirm"
              >
                Confirm position
              </button>
            )}
            {!isAnchor && (!placed || clip?.status === "needs_review") && (
              <button
                onClick={() => void useAnalyze.getState().openAiSync(selected)}
                title="Look for the clip's place from what was said, light changes and the camera clocks (⇧A)"
                data-testid="find-with-ai"
              >
                Find with AI
              </button>
            )}
            {!isAnchor && (
              <button
                onClick={() => void placeAtCursor(selected)}
                disabled={cursorS === null}
                title={
                  cursorS === null
                    ? "Click the timeline to set the cursor first"
                    : `Start the clip at ${formatTime(cursorS)}`
                }
                data-testid="place-at-cursor"
              >
                Place at cursor
              </button>
            )}
            {manual && (
              <button
                onClick={() => void correct("clear_offset", selected)}
                title="Forget the manual placement"
                data-testid="reset"
              >
                Reset to automatic
              </button>
            )}
            {excluded ? (
              <button onClick={() => void correct("include", selected)}>Include in sync</button>
            ) : (
              <button
                onClick={() => void correct("exclude", selected)}
                title="Leave this clip out of synchronisation"
                disabled={isReference}
              >
                Exclude
              </button>
            )}
            {hasAudio && !isReference && (
              <button
                onClick={() => void setReference(selected)}
                title="Place every other clip relative to this recording"
                data-testid="set-reference"
              >
                Use as reference
              </button>
            )}
          </div>
        </div>
      )}

      {alternatives.length > 0 && currentOffset !== null && anchor && (
        <div className="alternatives">
          <h3>Other positions that fit</h3>
          <ul>
            {alternatives.map((alt) => (
              <li key={alt.offset_s}>
                <span>{formatOffset(alt.offset_s - currentOffset)}</span>
                <span className="muted">{alt.n_inliers} windows agree</span>
                <button
                  className="small"
                  onClick={() => void correct("offset", selected, anchor.clip_id, alt.offset_s)}
                >
                  Use
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {clipMatches.length > 0 && (
        <div className="matches" data-testid="matches">
          <h3>Audio matches</h3>
          <p className="muted small">Offset = this clip's start minus the other clip's start.</p>
          <ul>
            {clipMatches.map((m) => (
              <MatchRow key={m.other_clip_id} clipId={selected} match={m} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
