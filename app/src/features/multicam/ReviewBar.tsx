// The review workflow ("Review N clips"): each clip that needs a look, one after another, beside the camera that
// overlaps it best (compare layout: reference left, clip under review right, the timeline below). Accept locks the
// clip where it is; Adjust leaves it to the nudges and sync points; Re-sync searches its audio again around the
// current position; Exclude leaves it out of the sync. Nothing is accepted automatically.
import { Check, ChevronLeft, ChevronRight, Play, RefreshCw, SlidersHorizontal, X, XCircle } from "lucide-react";

import { SyncBadge } from "../../design-system/components";
import { usePlayback } from "../../state/playback";
import { findClip, useApp } from "../../state/store";
import { clock } from "./clock";
import { audioMonitor } from "./MulticamViewer";

/** Open the review of every clip the engine flagged, most severe first. */
export function startReview(): void {
  const app = useApp.getState();
  const ids = (app.timeline?.review ?? []).map((r) => r.clip_id);
  if (ids.length === 0) {
    app.toast("info", "Nothing to review: every clip is placed confidently.");
    return;
  }
  usePlayback.getState().setReview({ ids, index: 0 });
  showReviewItem(0);
}

function showReviewItem(index: number): void {
  const pb = usePlayback.getState();
  const review = pb.review;
  if (!review) return;
  const k = Math.max(0, Math.min(review.ids.length - 1, index));
  pb.setReview({ ...review, index: k });
  const app = useApp.getState();
  const id = review.ids[k]!;
  const clip = findClip(app.timeline, id);
  if (clip?.group !== null && clip?.group !== undefined && clip.group !== app.group) app.setGroup(clip.group);
  app.select(id);
  app.reveal(id);
  pb.setLayout("compare");
  clock.pause();
  if (clip?.start_s !== null && clip?.start_s !== undefined)
    clock.seek(clip.start_s + Math.min(1, clip.duration_s / 4));
}

function stopReview(): void {
  const pb = usePlayback.getState();
  pb.setReview(null);
  pb.setLayout("grid");
}

export function ReviewBar() {
  const review = usePlayback((s) => s.review)!;
  const timeline = useApp((s) => s.timeline);
  const id = review.ids[review.index]!;
  const clip = findClip(timeline, id);
  const next = () => (review.index + 1 < review.ids.length ? showReviewItem(review.index + 1) : stopReview());
  const stillFlagged = timeline?.review.some((r) => r.clip_id === id) ?? false;

  return (
    <div className="mc-review" data-testid="review-bar">
      <span className="mc-review__count tnum">
        REVIEW {review.index + 1} / {review.ids.length}
      </span>
      <span className="mc-review__clip sy-ellipsis" title={clip?.path}>
        {clip ? `${clip.name} · ${clip.device_name}` : `Clip ${id}`}
      </span>
      {clip &&
        (stillFlagged ? (
          <SyncBadge status="review" confidence={clip.method === "audio" ? clip.confidence : null} />
        ) : (
          <SyncBadge status="high" label="Done" />
        ))}
      <span className="mc-review__grow" />
      <button
        type="button"
        onClick={() => showReviewItem(review.index - 1)}
        disabled={review.index === 0}
        data-testid="review-previous"
      >
        <ChevronLeft size={14} aria-hidden /> Previous
      </button>
      <button
        type="button"
        onClick={() => {
          audioMonitor();
          if (clip?.start_s !== null && clip?.start_s !== undefined) clock.seek(clip.start_s);
          clock.play();
        }}
        title="Play the reference and the clip together from the clip's start"
        data-testid="review-play"
      >
        <Play size={14} aria-hidden /> Play both
      </button>
      <button
        type="button"
        className="mc-review__accept"
        disabled={!clip || clip.start_s === null}
        onClick={() => void useApp.getState().confirm(id).then(next)}
        title="The clip is right where it is: lock it (confirmed)"
        data-testid="review-accept"
      >
        <Check size={14} aria-hidden /> Accept
      </button>
      <button
        type="button"
        onClick={() => {
          clock.pause();
          document.querySelector<HTMLElement>(".sy-si__nudges button")?.focus();
        }}
        title="Move it with the nudges (, and .), or set sync points (S)"
        data-testid="review-adjust"
      >
        <SlidersHorizontal size={14} aria-hidden /> Adjust
      </button>
      <button
        type="button"
        disabled={!clip || clip.start_s === null || !clip.has_audio}
        onClick={() => void useApp.getState().snap(id)}
        title="Search the audio again within ±2 s of where the clip is now"
        data-testid="review-resync"
      >
        <RefreshCw size={14} aria-hidden /> Re-sync
      </button>
      <button
        type="button"
        onClick={() => void useApp.getState().correct("exclude", id).then(next)}
        data-testid="review-exclude"
      >
        <XCircle size={14} aria-hidden /> Exclude
      </button>
      <button type="button" onClick={next} data-testid="review-next">
        Next <ChevronRight size={14} aria-hidden />
      </button>
      <button type="button" aria-label="Close the review" onClick={stopReview}>
        <X size={14} aria-hidden />
      </button>
    </div>
  );
}
