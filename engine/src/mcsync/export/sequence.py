"""Export model: a synchronised timeline group as an NLE sequence, in exact rational time.

Positions arrive from the timeline in float seconds. From there everything is a :class:`~fractions.Fraction`: frame
rates, frame numbers and in points. Each position is rounded once, and every rounding is reported.

Placement (docs/TECHNICAL_SPEC.md §8):

* A clip with video starts at its first frame on the nearest sequence frame, so it is at most half a frame early or
  late, as in any NLE.
* An audio-only clip (the external recorder) starts on the first sequence frame at or after its true start, and its
  in point absorbs the difference. Formats with sub-frame in points place it to the sample: FCPXML does, and so does
  Premiere Pro through the ticks in xmeml. Other xmeml readers round the in point to a frame, which is again at most
  half a frame off.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from fractions import Fraction

from mcsync.media.probe import AudioStreamInfo, MediaInfo
from mcsync.project.db import ClipRow
from mcsync.timecode import Timecode, parse_frame_rate, supports_drop_frame
from mcsync.timeline import Timeline, TimelineClip

DEFAULT_RATE = Fraction(25)
DEFAULT_SIZE = (1920, 1080)
SEQUENCE_SAMPLE_RATE = 48_000
_MICROSECOND = 1_000_000
_MAX_ROUNDING_OVERLAP_FRAMES = 2


class ExportError(ValueError):
    """The timeline cannot be exported as asked."""


@dataclass(frozen=True)
class ExportOptions:
    name: str = "Synchronised"
    #: Sequence frame rate; None picks the most common video rate among the exported clips.
    sequence_rate: Fraction | None = None
    #: Sequence start; ``;`` before the frames means drop-frame (29.97 and 59.94 only).
    start_timecode: str = "01:00:00:00"
    #: Timeline group: 0 is the reference and everything synced to it.
    group: int = 0
    #: Also export clips placed by uncertain matches or camera clocks only (they are listed as warnings).
    include_uncertain: bool = True


@dataclass(frozen=True)
class SourceMedia:
    """One media file, as both XML formats describe it."""

    key: str  # "file-1": stable within one export
    name: str
    path: str
    duration: Fraction  # seconds
    video_rate: Fraction | None
    width: int | None
    height: int | None
    video_frames: int | None
    audio_channels: int
    sample_rate: int
    bit_depth: int
    #: Start timecode of the file (frames at ``timecode_rate``), if it has one.
    timecode_frames: int | None
    timecode_rate: Fraction | None
    drop_frame: bool
    #: Where the file's own time starts, in seconds: its timecode, or its BWF time reference.
    #: FCPXML asset starts use it; xmeml in points count from the first frame instead.
    start: Fraction
    track_names: tuple[str, ...] = ()

    @property
    def has_video(self) -> bool:
        return self.video_rate is not None

    @property
    def is_ntsc(self) -> bool:
        return self.video_rate is not None and self.video_rate.denominator == 1001


@dataclass(frozen=True)
class ExportClip:
    clip_id: int
    name: str
    media: SourceMedia
    video_track: int | None  # 1-based; None for audio-only clips
    audio_tracks: tuple[int, ...]  # 1-based, one per channel
    start_frame: int  # sequence frames from the sequence start
    end_frame: int
    in_point: Fraction  # seconds from the file's first frame (or sample)
    true_start: Fraction  # where synchronisation put the clip's first frame, seconds from the sequence start
    status: str
    method: str
    drift_ppm: float = 0.0

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame

    def placed_start(self, rate: Fraction, *, subframe_in: bool) -> Fraction:
        """Where the clip's first frame ends up: exact in point, or the in point rounded to a frame."""
        in_point = self.in_point if subframe_in else Fraction(round(self.in_point * rate)) / rate
        return Fraction(self.start_frame) / rate - in_point

    def error(self, rate: Fraction, *, subframe_in: bool) -> Fraction:
        """Placement error in seconds (positive: later than synchronised)."""
        return self.placed_start(rate, subframe_in=subframe_in) - self.true_start


@dataclass(frozen=True)
class Track:
    index: int  # 1-based within its kind
    kind: str  # "video" | "audio"
    name: str
    device_name: str
    channel: int | None = None  # 1-based, audio tracks only


@dataclass(frozen=True)
class Skipped:
    clip_id: int
    name: str
    reason: str


@dataclass(frozen=True)
class ExportSequence:
    name: str
    rate: Fraction
    drop_frame: bool
    width: int
    height: int
    sample_rate: int
    start_frame: int  # timecode of the sequence start, in frames
    duration_frames: int
    video_tracks: tuple[Track, ...]
    audio_tracks: tuple[Track, ...]
    clips: tuple[ExportClip, ...]
    media: tuple[SourceMedia, ...]
    skipped: tuple[Skipped, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_ntsc(self) -> bool:
        return self.rate.denominator == 1001

    @property
    def start_timecode(self) -> Timecode:
        return Timecode.from_frames(self.start_frame, self.rate, self.drop_frame)

    def report(self, *, subframe_in: bool) -> dict:
        """What was exported and how exactly, for the user (JSON-ready)."""
        clips = []
        for c in sorted(self.clips, key=lambda c: (c.true_start, c.name)):
            err = c.error(self.rate, subframe_in=subframe_in)
            clips.append({
                "clip_id": c.clip_id,
                "name": c.name,
                "track": f"V{c.video_track}" if c.video_track else f"A{c.audio_tracks[0]}" if c.audio_tracks else "",
                "start_s": float(c.true_start),
                "placed_s": float(c.placed_start(self.rate, subframe_in=subframe_in)),
                "error_ms": round(float(err) * 1000, 3),
                "status": c.status,
            })  # fmt: skip
        errors = [abs(c["error_ms"]) for c in clips]
        return {
            "sequence": {
                "name": self.name,
                "rate": f"{self.rate.numerator}/{self.rate.denominator}",
                "width": self.width,
                "height": self.height,
                "start_timecode": str(self.start_timecode),
                "duration_s": float(Fraction(self.duration_frames) / self.rate),
                "video_tracks": len(self.video_tracks),
                "audio_tracks": len(self.audio_tracks),
            },
            "clips": clips,
            "max_error_ms": max(errors, default=0.0),
            "skipped": [{"clip_id": s.clip_id, "name": s.name, "reason": s.reason} for s in self.skipped],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def _seconds(value: float) -> Fraction:
    """Float seconds from the timeline as an exact value (to the microsecond, far below a sample)."""
    return Fraction(round(value * _MICROSECOND), _MICROSECOND)


def _round_half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def _export_stream(row: ClipRow) -> AudioStreamInfo | None:
    """The audio the NLE should get: the stream chosen for syncing, else the file's first."""
    return row.audio_stream_info or row.info.primary_audio


def source_media(key: str, info: MediaInfo, stream: AudioStreamInfo | None) -> SourceMedia:
    video = info.primary_video
    rate = video.frame_rate if video else None
    duration = _seconds(info.duration_s)
    frames = None
    if video and rate:
        frames = video.frame_count or max(1, math.floor(duration * rate))
    tc = info.timecode
    tc_rate = tc.rate if tc and tc.rate else None
    tc_frames = round(tc.seconds * tc_rate) if tc and tc_rate else None
    sample_rate = stream.sample_rate if stream else SEQUENCE_SAMPLE_RATE
    if video is None and info.bwf and info.bwf.time_reference is not None and stream:
        start = Fraction(info.bwf.time_reference, stream.sample_rate)  # to the sample, as NLEs read it
    elif tc_frames is not None and tc_rate is not None:
        start = Fraction(tc_frames) / tc_rate
    else:
        start = Fraction(0)
    return SourceMedia(
        key=key,
        name=info.path.replace("\\", "/").rsplit("/", 1)[-1],  # Windows paths too, whatever the OS
        path=info.path,
        duration=duration,
        video_rate=rate,
        width=video.width if video else None,
        height=video.height if video else None,
        video_frames=frames,
        audio_channels=stream.channels if stream else 0,
        sample_rate=sample_rate,
        bit_depth=stream.bits_per_sample if stream and stream.bits_per_sample else 16,
        timecode_frames=tc_frames,
        timecode_rate=tc_rate,
        drop_frame=bool(tc and tc.drop_frame),
        start=start,
        track_names=info.bwf.track_names if info.bwf else (),
    )


def media_frames(src: SourceMedia, rate: Fraction) -> int:
    """How many whole frames of ``rate`` the file lasts."""
    if src.video_rate == rate and src.video_frames:
        return src.video_frames
    return math.floor(src.duration * rate)


def choose_rate(clips: list[tuple[TimelineClip, ClipRow]]) -> Fraction:
    """The most common video frame rate, weighted by duration; 25 fps for audio-only projects."""
    weight: Counter[Fraction] = Counter()
    for clip, row in clips:
        rate = row.info.frame_rate
        if rate is not None and clip.has_video:
            weight[rate] += clip.duration_s
    return max(weight, key=lambda r: (weight[r], r)) if weight else DEFAULT_RATE


def _choose_size(clips: list[tuple[TimelineClip, ClipRow]], rate: Fraction) -> tuple[int, int]:
    sizes: Counter[tuple[int, int]] = Counter()
    for _, row in clips:
        video = row.info.primary_video
        if video and video.width and video.height:
            # Clips at the sequence rate decide; others only break ties.
            sizes[(video.width, video.height)] += 1000 if video.frame_rate == rate else 1
    return max(sizes, key=lambda s: (sizes[s], s)) if sizes else DEFAULT_SIZE


def _sequence_start(options: ExportOptions, rate: Fraction) -> tuple[int, bool]:
    try:
        tc = Timecode.parse(options.start_timecode)
        if tc.drop_frame and not supports_drop_frame(rate):
            tc = replace(tc, drop_frame=False)
        return tc.to_frames(rate), tc.drop_frame
    except ValueError as exc:
        raise ExportError(f"invalid start timecode {options.start_timecode!r}: {exc}") from exc


def _skip_reason(clip: TimelineClip, options: ExportOptions) -> str | None:
    if clip.media_status != "online":
        return f"media {clip.media_status}"
    if clip.status == "needs_review" and not options.include_uncertain:
        return "uncertain placement (not included)"
    return None


def build_sequence(
    timeline: Timeline, rows: Mapping[int, ClipRow], options: ExportOptions | None = None
) -> ExportSequence:
    """Lay out one timeline group as an NLE sequence."""
    options = options or ExportOptions()
    group = next((g for g in timeline.groups if g.group == options.group), None)
    if group is None:
        if not timeline.groups:
            raise ExportError("nothing to export: synchronise first")
        raise ExportError(f"the timeline has no group {options.group}")

    skipped: list[Skipped] = []
    for g in timeline.groups:
        if g.group != options.group:
            reason = f"not linked to the exported group (group {g.group})"
            skipped += [Skipped(c.clip_id, c.name, reason) for c in g.clips]
    skipped += [Skipped(c.clip_id, c.name, "not placed") for c in timeline.unsynced]

    chosen: list[tuple[TimelineClip, ClipRow]] = []
    for clip in group.clips:
        reason = _skip_reason(clip, options)
        if reason:
            skipped.append(Skipped(clip.clip_id, clip.name, reason))
        elif clip.clip_id not in rows:
            skipped.append(Skipped(clip.clip_id, clip.name, "missing from the project"))
        else:
            chosen.append((clip, rows[clip.clip_id]))
    if not chosen:
        raise ExportError("no clips to export")

    rate = parse_frame_rate(options.sequence_rate) if options.sequence_rate else choose_rate(chosen)
    width, height = _choose_size(chosen, rate)
    start_frame, drop_frame = _sequence_start(options, rate)

    # One media entry per file, in timeline order.
    media: dict[str, SourceMedia] = {}
    for _, row in chosen:
        if row.path not in media:
            media[row.path] = source_media(f"file-{len(media) + 1}", row.info, _export_stream(row))

    # Tracks follow the timeline's device tracks: cameras first, then recorders.
    video_tracks: list[Track] = []
    audio_tracks: list[Track] = []
    video_index: dict[int, int] = {}
    audio_index: dict[int, list[int]] = {}
    by_track: dict[int, list[tuple[TimelineClip, ClipRow]]] = {}
    for clip, row in chosen:
        by_track.setdefault(clip.track if clip.track is not None else -1, []).append((clip, row))
    for t in group.tracks:
        members = by_track.get(t.index, [])
        if not members:
            continue
        name = t.device_name if t.lane == 0 else f"{t.device_name} ({t.lane + 1})"
        if any(clip.has_video and media[row.path].has_video for clip, row in members):
            video_tracks.append(Track(len(video_tracks) + 1, "video", name, t.device_name))
            video_index[t.index] = len(video_tracks)
        channels = max(media[row.path].audio_channels for _, row in members)
        names = next((media[row.path].track_names for _, row in members if media[row.path].track_names), ())
        audio_index[t.index] = []
        for ch in range(channels):
            label = names[ch] if ch < len(names) else str(ch + 1)
            audio_tracks.append(Track(len(audio_tracks) + 1, "audio", f"{name} {label}", t.device_name, ch + 1))
            audio_index[t.index].append(len(audio_tracks))

    warnings: list[str] = []
    clips: list[ExportClip] = []
    for clip, row in chosen:
        src = media[row.path]
        true_start = _seconds(clip.start_s)  # type: ignore[arg-type]
        video = clip.has_video and src.has_video
        if video:
            first = _round_half_up(true_start * rate)
            in_point = Fraction(0)
            last = first + media_frames(src, rate)
        else:
            # Trim to the next frame boundary; the in point carries the sub-frame part, to the sample.
            first = math.ceil(true_start * rate)
            in_point = Fraction(first) / rate - true_start
            in_point = Fraction(round(in_point * src.sample_rate), src.sample_rate)
            last = math.floor((true_start + src.duration) * rate)
        if last <= first:
            skipped.append(Skipped(clip.clip_id, clip.name, "shorter than one sequence frame"))
            continue
        track_audio = audio_index.get(clip.track if clip.track is not None else -1, [])
        clips.append(
            ExportClip(
                clip_id=clip.clip_id,
                name=clip.name,
                media=src,
                video_track=video_index.get(clip.track) if video and clip.track is not None else None,
                audio_tracks=tuple(track_audio[: src.audio_channels]),
                start_frame=first,
                end_frame=last,
                in_point=in_point,
                true_start=true_start,
                status=clip.status,
                method=clip.method,
                drift_ppm=clip.drift_ppm,
            )
        )
        if clip.status == "needs_review":
            how = _METHOD_TEXT.get(clip.method, clip.method)
            warnings.append(f"{clip.name}: placed by {how}; check it in the NLE.")
        if row.info.primary_video and row.info.primary_video.is_vfr:
            warnings.append(f"{clip.name}: variable frame rate; the NLE may conform it and shift it slightly.")
        half_span = abs(clip.drift_ppm) * 1e-6 * clip.duration_s / 2
        if half_span > 0.5 / float(rate):
            warnings.append(
                f"{clip.name}: its clock drifts {abs(clip.drift_ppm):.1f} ppm; it is aligned in the middle and "
                f"{half_span * 1000:.0f} ms off at its ends."
            )

    clips = _resolve_rounding_overlaps(clips, warnings)
    duration = max(c.end_frame for c in clips)
    return ExportSequence(
        name=options.name,
        rate=rate,
        drop_frame=drop_frame,
        width=width,
        height=height,
        sample_rate=SEQUENCE_SAMPLE_RATE,
        start_frame=start_frame,
        duration_frames=duration,
        video_tracks=tuple(video_tracks),
        audio_tracks=tuple(audio_tracks),
        clips=tuple(sorted(clips, key=lambda c: (c.start_frame, c.clip_id))),
        media=tuple(media.values()),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )


_METHOD_TEXT = {
    "audio": "an uncertain audio match",
    "metadata": "the camera clock only",
    "timecode": "timecode that disagrees with the audio",
    "manual": "hand",
}


def _resolve_rounding_overlaps(clips: list[ExportClip], warnings: list[str]) -> list[ExportClip]:
    """Back-to-back clips of one device can overlap by a frame after rounding: trim the earlier one."""
    out = {c.clip_id: c for c in clips}
    lanes: dict[tuple[str, int], list[int]] = {}
    for c in clips:
        keys = [("video", c.video_track)] if c.video_track else []
        keys += [("audio", t) for t in c.audio_tracks]
        for key in keys:
            lanes.setdefault(key, []).append(c.clip_id)  # type: ignore[arg-type]
    reported: set[tuple[int, int]] = set()  # one warning per pair, not per shared track
    for ids in lanes.values():
        ordered = sorted((out[i] for i in ids), key=lambda c: (c.start_frame, c.clip_id))
        for prev, cur in zip(ordered, ordered[1:], strict=False):
            prev = out[prev.clip_id]
            overlap = prev.end_frame - cur.start_frame
            if overlap <= 0:
                continue
            if overlap <= _MAX_ROUNDING_OVERLAP_FRAMES and cur.start_frame > prev.start_frame:
                out[prev.clip_id] = replace(prev, end_frame=cur.start_frame)
            elif (prev.clip_id, cur.clip_id) not in reported:
                reported.add((prev.clip_id, cur.clip_id))
                warnings.append(f"{prev.name} and {cur.name} overlap on one track by {overlap} frames.")
    return list(out.values())
