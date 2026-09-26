"""Project file persistence and JSON round trips (no FFmpeg needed)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from fractions import Fraction

import pytest

from mcsync.media.devices import DeviceGuess
from mcsync.media.library import MediaItem
from mcsync.media.probe import AudioStreamInfo, MediaInfo, TimecodeInfo, VideoStreamInfo
from mcsync.media.riff import BwfMetadata
from mcsync.project import Project, ProjectError
from mcsync.serialize import match_from_dict, media_info_from_dict, media_info_to_dict, to_jsonable
from mcsync.sync.types import (
    Candidate,
    ClipPlacement,
    Flag,
    MatchStatus,
    OffsetEstimate,
    PairwiseMatch,
    PlacementMethod,
    PlacementStatus,
    SyncResult,
    WindowMeasurement,
)


def media(path, *, audio_only=False, created=None, tc=None, size=1000):
    video = (
        () if audio_only else (VideoStreamInfo(0, "h264", 1920, 1080, Fraction(24000, 1001), None, False, 0.0, 60.0),)
    )
    return MediaInfo(
        path=str(path), size_bytes=size, mtime_ns=0, container="mov", duration_s=60.0, video=video,
        audio=(AudioStreamInfo(1 if video else 0, "aac", 48000, 2, "stereo", 0.021, 60.0),),
        timecode=tc, creation_time=created, make="Canon", model="EOS R6",
        bwf=BwfMetadata(time_reference=1_728_000_000, timecode_rate=Fraction(25), track_names=("lav",)),
        raw={"format": {"format_name": "mov"}},
    )  # fmt: skip


def item(path, device="camA", **kw):
    info = media(path, **kw)
    return MediaItem(info=info, fingerprint=f"fp-{path}", device=DeviceGuess(device, device.upper(), "camera"),
                     audio_stream=info.audio[0])  # fmt: skip


@pytest.fixture
def project(tmp_path):
    with Project.create(tmp_path / "wedding.mcsync", "Smith wedding") as p:
        yield p


def test_create_open_and_settings(tmp_path):
    path = tmp_path / "p.mcsync"
    with Project.create(path, "Smith wedding") as p:
        assert p.name == "Smith wedding"
        assert p.settings() == {}
        assert p.update_settings(mode="audio", timecode_jam_synced=True) == {
            "mode": "audio",
            "timecode_jam_synced": True,
        }
    with Project.open(path) as p:
        assert p.settings()["mode"] == "audio"
    with pytest.raises(ProjectError):
        Project.create(path)
    with pytest.raises(ProjectError):
        Project.open(tmp_path / "missing.mcsync")
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE x (y)").connection.close()
    with pytest.raises(ProjectError):
        Project.open(other)


def test_media_clips_and_devices(project, tmp_path):
    ids = project.add_media([item(tmp_path / "A001.MOV"), item(tmp_path / "A002.MOV"), item(tmp_path / "r.wav", "rec")])
    assert ids == [1, 2, 3]
    clips = project.clips()
    assert [c.name for c in clips] == ["A001.MOV", "A002.MOV", "r.wav"]
    assert clips[0].info == media(tmp_path / "A001.MOV")  # full metadata survives the round trip
    assert clips[0].engine_id == "1" and clips[0].audio_stream == 1
    assert {d["key"] for d in project.devices()} == {"camA", "rec"}
    # Re-importing a file updates it in place.
    assert project.add_media([item(tmp_path / "A001.MOV", "camB")]) == [1]
    assert project.clip(1).device_key == "camB"
    project.set_clip_audio(1, 1, 0)
    assert (project.clip(1).audio_stream, project.clip(1).audio_channel) == (1, 0)
    project.remove_clips([3])
    assert [c.id for c in project.clips()] == [1, 2]
    assert "rec" not in {d["key"] for d in project.devices()}  # orphaned device removed
    with pytest.raises(ProjectError):
        project.clip(99)
    media_item = project.clip(2).to_media_item()
    assert media_item.audio_stream.index == 1 and media_item.device.key == "camA"


def test_media_status_on_reopen(tmp_path):
    present = tmp_path / "present.mov"
    present.write_bytes(b"x")
    path = tmp_path / "p.mcsync"
    with Project.create(path) as p:
        info = media(present)
        st = present.stat()
        info = MediaInfo(**{**info.__dict__, "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns})
        p.add_media([MediaItem(info=info, fingerprint="f", device=DeviceGuess("d", "D", "camera"), audio_stream=None)])
        p.add_media([item(tmp_path / "gone.mov")])
    with Project.open(path) as p:
        assert {c.name: c.status for c in p.clips()} == {"present.mov": "online", "gone.mov": "offline"}
    present.write_bytes(b"edited")
    with Project.open(path) as p:
        assert p.clip(1).status == "changed"


def test_correction_log_replay_undo_redo(project, tmp_path):
    project.add_media([item(tmp_path / f"{k}.mov") for k in range(3)])
    project.add_correction("offset", 2, other_clip_id=1, offset_s=10.0)
    project.add_correction("offset", 2, other_clip_id=1, offset_s=12.5)  # dragged again: the latest wins
    project.add_correction("reject_pair", 1, other_clip_id=3)
    project.add_correction("exclude", 3)
    c = project.corrections()
    assert [(o.clip_id, o.anchor_clip_id, o.offset_s) for o in c.offsets] == [("2", "1", 12.5)]
    assert c.is_rejected("3", "1") and c.excluded_clips == {"3"}

    assert project.undo() and project.undo()  # exclusion and rejection undone
    c = project.corrections()
    assert not c.rejected_pairs and not c.excluded_clips
    assert project.redo()
    assert project.corrections().is_rejected("1", "3")
    project.add_correction("clear_offset", 2)  # a new edit discards what is left to redo
    assert not project.redo()
    c = project.corrections()
    assert c.offsets == [] and c.is_rejected("1", "3") and not c.excluded_clips
    project.add_correction("unreject_pair", 1, other_clip_id=3)
    project.add_correction("exclude", 2)
    project.add_correction("include", 2)
    assert project.corrections().rejected_pairs == set() and project.corrections().excluded_clips == set()
    assert len(project.correction_log()) == 7
    while project.undo():
        pass
    assert not project.undo()
    for bad in (dict(kind="teleport", clip_id=1), dict(kind="offset", clip_id=1), dict(kind="reject_pair", clip_id=1)):
        with pytest.raises(ProjectError):
            project.add_correction(**bad)


def sample_match(ref="1", tgt="2") -> PairwiseMatch:
    est = OffsetEstimate(
        offset_s=12.5, confidence=0.93, status=MatchStatus.CONFIDENT, offset_time_s=30.0, drift_ppm=12.0,
        drift_std_ppm=float("inf"), std_error_s=1e-5, overlap_s=60.0, coarse_psr=22.0, uniqueness=float("inf"),
        n_windows=3, inlier_fraction=1.0, correlation=0.4, flags=(Flag.DRIFT,),
        windows=(WindowMeasurement(5.0, 12.5, 0.4, True),), alternatives=(Candidate(40.0, 6.0, 1, 0.3),),
    )  # fmt: skip
    return PairwiseMatch(ref, tgt, 12.479, est, offset_time_s=30.021, audio_shift_s=0.021, search_window=(10.0, 15.0),
                         flags=(Flag.CLOCK_MISMATCH,))  # fmt: skip


def test_matches_runs_and_placements(project, tmp_path):
    project.add_media([item(tmp_path / f"{k}.mov") for k in range(2)])
    run = project.start_run({"mode": "hybrid"})
    project.save_match(run, "key-a", sample_match())
    assert project.run_matches(run) == [sample_match()]
    assert project.find_matches(["key-a", "key-b"]) == {"key-a": sample_match()}
    assert project.last_completed_run() is None
    project.finish_run(run, "completed")
    assert project.last_completed_run() == run
    second = project.start_run({})
    assert project.runs()[-1]["status"] == "running"
    third = project.start_run({})  # a new run marks an abandoned one as failed
    assert [r["status"] for r in project.runs()] == ["completed", "failed", "running"]
    assert third == second + 1

    placement = ClipPlacement("2", 12.479, 0, PlacementMethod.AUDIO, 0.93, PlacementStatus.SYNCED, (Flag.DRIFT,), 12.0)
    reference = ClipPlacement("1", 0.0, 0, PlacementMethod.REFERENCE, 1.0, PlacementStatus.SYNCED)
    project.save_placements(SyncResult("1", {"1": reference, "2": placement}, [], []))
    assert project.placements() == {1: reference, 2: placement}


def test_json_round_trips():
    match = sample_match()
    data = to_jsonable(match)
    assert data["estimate"]["drift_std_ppm"] is None  # Infinity is not JSON
    assert match_from_dict(data) == match
    info = media(
        "/x/A.MOV",
        created=datetime(2026, 6, 14, 10, tzinfo=UTC),
        tc=TimecodeInfo(3603.6, Fraction(24000, 1001), False, "tmcd", "01:00:00:00"),
    )
    restored = media_info_from_dict(media_info_to_dict(info))
    assert restored == info and restored.raw == info.raw
    assert media_info_to_dict(info, include_raw=False).get("raw") is None
    with pytest.raises(TypeError):
        to_jsonable(object())
