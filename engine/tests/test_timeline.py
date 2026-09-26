"""Timeline model: groups, per-device tracks, overflow lanes, review queue."""

from __future__ import annotations

from fractions import Fraction

import pytest

from mcsync.media.probe import AudioStreamInfo, MediaInfo, VideoStreamInfo
from mcsync.project.db import ClipRow
from mcsync.sync.types import ClipPlacement, Flag, PlacementMethod, PlacementStatus
from mcsync.timeline import REVIEW_REASONS, build_timeline


def row(clip_id, device_id, kind="camera", duration=60.0, status="online"):
    video = (
        () if kind == "recorder" else (VideoStreamInfo(0, "h264", 64, 36, Fraction(25), None, False, 0.0, duration),)
    )
    info = MediaInfo(f"/m/{clip_id}.mov", 1, 0, "mov", duration, video,
                     (AudioStreamInfo(1, "aac", 48000, 2, None, 0.0, duration),))  # fmt: skip
    return ClipRow(clip_id, f"{clip_id}.mov", clip_id, info.path, "fp", status, info, device_id, f"dev{device_id}",
                   f"Device {device_id}", kind, 1, None, None, None, None)  # fmt: skip


def placed(clip_id, start, group=0, status=PlacementStatus.SYNCED, method=PlacementMethod.AUDIO, flags=(), conf=1.0):
    return ClipPlacement(str(clip_id), start, group, method, conf, status, tuple(flags))


def test_groups_tracks_and_origin():
    rows = [row(1, 10, "recorder", 600), row(2, 20), row(3, 20), row(4, 30), row(5, 40), row(6, 40)]
    placements = {
        1: placed(1, 0.0, method=PlacementMethod.REFERENCE),
        2: placed(2, -5.0),  # starts before the reference
        3: placed(3, 100.0),
        4: placed(4, 30.0),
        5: placed(5, 0.0, group=1, status=PlacementStatus.NEEDS_REVIEW, flags=(Flag.DETACHED_GROUP,)),
        6: placed(6, 70.0, group=1, status=PlacementStatus.NEEDS_REVIEW, flags=(Flag.DETACHED_GROUP,)),
    }
    tl = build_timeline(rows, placements, reference_clip_id=1)
    main, second = tl.groups
    assert main.origin_offset_s == 5.0  # timeline zero is the camera that started first
    assert tl.clip(2).start_s == 0.0 and tl.clip(1).start_s == 5.0
    assert main.duration_s == pytest.approx(605.0)
    # Cameras first, recorders last; one track per device.
    assert [(t.device_id, t.lane) for t in main.tracks] == [(20, 0), (30, 0), (10, 0)]
    assert tl.clip(2).track == tl.clip(3).track == 0 and tl.clip(1).track == 2
    assert [c.clip_id for c in second.clips] == [5, 6]
    assert [(r.clip_id, r.reason) for r in tl.review] == [(5, "detached"), (6, "detached")]
    assert tl.stats == {"clips": 6, "synced": 4, "needs_review": 2, "unsynced": 0}


def test_overlapping_clips_of_one_device_get_a_lane_and_a_review():
    rows = [row(1, 10, "recorder", 600), row(2, 20), row(3, 20)]
    placements = {1: placed(1, 0.0), 2: placed(2, 10.0), 3: placed(3, 40.0)}  # 3 starts before 2 ends
    tl = build_timeline(rows, placements)
    lanes = {t.index: t.lane for t in tl.groups[0].tracks}
    assert lanes[tl.clip(2).track] == 0 and lanes[tl.clip(3).track] == 1
    assert [(r.clip_id, r.reason) for r in tl.review] == [(3, "device_overlap")]


def test_review_queue_order():
    rows = [row(k, k) for k in range(1, 8)]
    rows[6] = row(7, 7, status="offline")
    placements = {
        1: placed(1, 0.0, method=PlacementMethod.REFERENCE),
        2: placed(2, 5.0, status=PlacementStatus.NEEDS_REVIEW, conf=0.5),
        3: placed(3, 6.0, status=PlacementStatus.NEEDS_REVIEW, method=PlacementMethod.METADATA, conf=0.3),
        4: placed(4, 7.0, status=PlacementStatus.NEEDS_REVIEW, flags=(Flag.TIMECODE_DISAGREES,)),
        5: ClipPlacement("5", None, None, PlacementMethod.NONE, 0.0, PlacementStatus.UNSYNCED),
        6: ClipPlacement("6", None, None, PlacementMethod.NONE, 0.0, PlacementStatus.UNSYNCED, (Flag.EXCLUDED,)),
        7: placed(7, 8.0),
    }
    tl = build_timeline(rows, placements)
    reasons = [(r.clip_id, r.reason) for r in tl.review]
    assert reasons == [(4, "conflict"), (2, "uncertain"), (3, "metadata_only"), (5, "unsynced"), (7, "offline")]
    assert [REVIEW_REASONS.index(r) for _, r in reasons] == sorted(REVIEW_REASONS.index(r) for _, r in reasons)
    assert {c.clip_id for c in tl.unsynced} == {5, 6}
    with pytest.raises(KeyError):
        tl.clip(99)


def test_clips_without_placements_and_empty_projects():
    tl = build_timeline([row(1, 1)], {})
    assert tl.groups == () and [c.clip_id for c in tl.unsynced] == [1]
    assert build_timeline([], {}).stats["clips"] == 0
