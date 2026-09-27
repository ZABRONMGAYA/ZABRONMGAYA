// Plain-language explanations of engine flags, statuses and review reasons.
import type { ReviewReason, TimelineClip } from "../api/contract";

export const REASON_LABELS: Record<ReviewReason, string> = {
  conflict: "Measurements disagree",
  device_overlap: "Overlaps another clip of its device",
  detached: "Not linked to the reference",
  uncertain: "Uncertain audio match",
  metadata_only: "Placed by camera clock only",
  unsynced: "Not placed",
  offline: "Media offline",
};

export const FLAG_LABELS: Record<string, string> = {
  silent: "The audio is silent or too quiet to match.",
  silent_overlap: "The overlapping part is silent.",
  no_overlap: "Too short to overlap anything by 3 s or more.",
  no_correlation: "No other recording sounds like this one.",
  ambiguous: "The audio repeats (a loop or the same song twice): several positions fit equally well.",
  inconsistent_windows: "Only part of the clip agrees with the match.",
  unverified: "Too little audio to double-check the match.",
  short_overlap: "The overlap with other recordings is short.",
  drift: "This device's clock runs at a different speed; alignment is best in the middle of the clip.",
  clock_mismatch: "The audio was found away from where the timecode said.",
  rejected_inconsistent: "An audio match contradicted the others and was ignored.",
  redundant_uncertain: "An uncertain audio match between clips already placed by stronger evidence (not used).",
  conflicting_matches: "A confident audio match had to be ignored because it contradicts the others.",
  timecode_disagrees: "The clip's timecode disagrees with its audio; the audio was trusted.",
  detached_group: "Synced with some clips, but not connected to the reference recording.",
  manual: "Placed by hand.",
  manual_conflict: "Two manual placements contradict each other; the later one was ignored.",
  excluded: "Excluded from synchronisation.",
  no_audio: "The clip has no audio: placed by timecode or clock, or by hand.",
  user_rejected: "You rejected this match.",
  below_threshold: "The match was too weak to use.",
};

export const METHOD_LABELS: Record<TimelineClip["method"], string> = {
  reference: "Reference",
  audio: "Audio",
  timecode: "Timecode",
  metadata: "Camera clock",
  chapter: "Chapter",
  manual: "Manual",
  none: "—",
};

export const STATUS_LABELS: Record<TimelineClip["status"], string> = {
  synced: "Synced",
  needs_review: "Needs review",
  unsynced: "Not synced",
};

export function driftSummary(clip: Pick<TimelineClip, "drift_ppm" | "duration_s">): string | null {
  if (Math.abs(clip.drift_ppm) < 0.5) return null;
  const halfSpan = (Math.abs(clip.drift_ppm) * 1e-6 * clip.duration_s) / 2;
  const direction = clip.drift_ppm > 0 ? "fast" : "slow";
  return `Clock runs ${Math.abs(clip.drift_ppm).toFixed(1)} ppm ${direction}: ±${(halfSpan * 1000).toFixed(1)} ms at the clip's ends`;
}
