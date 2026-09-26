"""Timeline model: what the UI draws and what export writes.

A timeline is built from the project's clips and the solver's placements:

* **groups**: group 0 holds the reference and everything synced to it; groups 1..n were synced among themselves but
  not to the reference. Positions are shifted so each group's earliest clip starts at 0.
* **tracks**: one per device and group; cameras, phones and drones first, recorders last. One device's clips never
  overlap. If they do, the device assignment is wrong, and the overlapping clip goes to an extra lane of that device
  and into the review queue.
* **review queue**: every clip the editor should look at, most severe first.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from mcsync.project.db import ClipRow
from mcsync.sync.types import ClipPlacement, Flag, PlacementMethod, PlacementStatus

_KIND_ORDER = {"camera": 0, "phone": 1, "drone": 2, "other": 3, "recorder": 4}
_OVERLAP_TOLERANCE_S = 0.01

# Review reasons, most severe first. The UI explains each key.
REVIEW_REASONS = (
    "conflict",  # measurements disagree (audio vs audio, audio vs timecode)
    "device_overlap",  # two clips of one device overlap: wrong device assignment
    "detached",  # synced to each other but not to the reference
    "uncertain",  # placed by a match that is not confident
    "metadata_only",  # placed only by camera clock / creation time (±1 s)
    "unsynced",  # not placed at all
    "offline",  # media file missing or changed since import
)


@dataclass(frozen=True)
class TimelineClip:
    clip_id: int
    name: str
    path: str
    device_id: int | None
    device_name: str
    kind: str
    duration_s: float
    has_video: bool
    has_audio: bool
    frame_rate: str | None
    timecode: str | None
    media_status: str
    start_s: float | None = None
    group: int | None = None
    track: int | None = None
    status: str = PlacementStatus.UNSYNCED.value
    method: str = PlacementMethod.NONE.value
    confidence: float = 0.0
    flags: tuple[str, ...] = ()
    drift_ppm: float = 0.0

    @property
    def end_s(self) -> float | None:
        return None if self.start_s is None else self.start_s + self.duration_s


@dataclass(frozen=True)
class TimelineTrack:
    index: int
    device_id: int | None
    device_name: str
    kind: str
    lane: int  # 0 = the device's main track; >0 = overflow for overlapping clips


@dataclass(frozen=True)
class TimelineGroup:
    group: int
    duration_s: float
    #: How far the group's timeline zero is from the anchor clip's start (the
    #: reference clip for group 0): ``timeline position = placement + origin_offset``.
    origin_offset_s: float
    tracks: tuple[TimelineTrack, ...]
    clips: tuple[TimelineClip, ...]


@dataclass(frozen=True)
class ReviewItem:
    clip_id: int
    reason: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Timeline:
    groups: tuple[TimelineGroup, ...]
    unsynced: tuple[TimelineClip, ...]
    review: tuple[ReviewItem, ...]
    reference_clip_id: int | None = None
    stats: dict = field(default_factory=dict)

    def clip(self, clip_id: int) -> TimelineClip:
        for g in self.groups:
            for c in g.clips:
                if c.clip_id == clip_id:
                    return c
        for c in self.unsynced:
            if c.clip_id == clip_id:
                return c
        raise KeyError(clip_id)


def _base_clip(row: ClipRow) -> TimelineClip:
    info = row.info
    return TimelineClip(
        clip_id=row.id,
        name=row.name,
        path=row.path,
        device_id=row.device_id,
        device_name=row.device_name or row.name,
        kind=row.device_kind or ("recorder" if info.is_audio_only else "camera"),
        duration_s=info.duration_s,
        has_video=not info.is_audio_only,
        has_audio=row.audio_stream is not None,
        frame_rate=f"{info.frame_rate.numerator}/{info.frame_rate.denominator}" if info.frame_rate else None,
        timecode=info.timecode.text if info.timecode else None,
        media_status=row.status,
    )


def _review_reason(clip: TimelineClip, overlapping: bool) -> str | None:
    flags = set(clip.flags)
    if {Flag.CONFLICTING_MATCHES.value, Flag.TIMECODE_DISAGREES.value} & flags:
        return "conflict"
    if overlapping:
        return "device_overlap"
    if clip.status == PlacementStatus.UNSYNCED.value and Flag.EXCLUDED.value not in flags:
        return "unsynced"
    if Flag.DETACHED_GROUP.value in flags:
        return "detached"
    if clip.status == PlacementStatus.NEEDS_REVIEW.value:
        return "metadata_only" if clip.method == PlacementMethod.METADATA.value else "uncertain"
    if clip.media_status != "online":
        return "offline"
    return None


def build_timeline(
    clips: Sequence[ClipRow],
    placements: dict[int, ClipPlacement],
    reference_clip_id: int | None = None,
) -> Timeline:
    by_group: dict[int, list[TimelineClip]] = defaultdict(list)
    unsynced: list[TimelineClip] = []
    for row in clips:
        base = _base_clip(row)
        p = placements.get(row.id)
        if p is None:
            unsynced.append(base)
            continue
        placed = TimelineClip(
            **{
                **base.__dict__,
                "start_s": p.start_s,
                "group": p.group,
                "status": p.status.value,
                "method": p.method.value,
                "confidence": p.confidence,
                "flags": tuple(f.value for f in p.flags),
                "drift_ppm": p.drift_ppm,
            }
        )
        if p.start_s is None or p.group is None:
            unsynced.append(placed)
        else:
            by_group[p.group].append(placed)

    groups: list[TimelineGroup] = []
    overlapping: set[int] = set()
    for g in sorted(by_group):
        members = by_group[g]
        origin = min(c.start_s for c in members)  # type: ignore[type-var]
        members = [TimelineClip(**{**c.__dict__, "start_s": c.start_s - origin}) for c in members]  # type: ignore[operator]
        devices: dict[int | None, list[TimelineClip]] = defaultdict(list)
        for c in members:
            devices[c.device_id].append(c)
        order = sorted(devices, key=lambda d: (_KIND_ORDER.get(devices[d][0].kind, 3), devices[d][0].device_name))
        tracks: list[TimelineTrack] = []
        placed: list[TimelineClip] = []
        for device_id in order:
            lanes: list[float] = []  # end time of the last clip in each lane
            for c in sorted(devices[device_id], key=lambda c: (c.start_s, c.clip_id)):
                lane = next((k for k, end in enumerate(lanes) if c.start_s >= end - _OVERLAP_TOLERANCE_S), None)  # type: ignore[operator]
                if lane is None:
                    lane = len(lanes)
                    lanes.append(0.0)
                    first = devices[device_id][0]
                    tracks.append(TimelineTrack(len(tracks), device_id, first.device_name, first.kind, lane))
                if lane > 0:
                    overlapping.add(c.clip_id)
                lanes[lane] = c.end_s  # type: ignore[assignment]
                track = next(t.index for t in tracks if t.device_id == device_id and t.lane == lane)
                placed.append(TimelineClip(**{**c.__dict__, "track": track}))
        duration = max(c.end_s for c in placed)  # type: ignore[type-var]
        groups.append(
            TimelineGroup(
                group=g,
                duration_s=duration,  # type: ignore[arg-type]
                origin_offset_s=-origin,  # type: ignore[operator]
                tracks=tuple(tracks),
                clips=tuple(sorted(placed, key=lambda c: (c.track, c.start_s))),  # type: ignore[arg-type]
            )
        )

    review: list[ReviewItem] = []
    for c in [c for g in groups for c in g.clips] + unsynced:
        reason = _review_reason(c, c.clip_id in overlapping)
        if reason is not None:
            review.append(ReviewItem(c.clip_id, reason, c.flags))
    review.sort(key=lambda r: (REVIEW_REASONS.index(r.reason), r.clip_id))

    all_clips = [c for g in groups for c in g.clips] + unsynced
    stats = {
        "clips": len(all_clips),
        "synced": sum(c.status == PlacementStatus.SYNCED.value for c in all_clips),
        "needs_review": sum(c.status == PlacementStatus.NEEDS_REVIEW.value for c in all_clips),
        "unsynced": len(unsynced),
    }
    return Timeline(tuple(groups), tuple(unsynced), tuple(review), reference_clip_id, stats)
