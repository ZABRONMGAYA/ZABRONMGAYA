"""Real files generated with FFmpeg, run through probe → extract → sync."""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from mcsync.media import (
    AnalysisCache,
    ExtractionCancelled,
    ExtractionError,
    build_clip_inputs,
    extract_audio,
    extract_to_cache,
    fingerprint,
    probe,
    scan_media,
)
from mcsync.media.extract import ffmpeg_command
from mcsync.media.tools import find_tools
from mcsync.sync import (
    ClockSource,
    PlacementMethod,
    PlacementStatus,
    SyncEngine,
    SyncOptions,
    estimate_offset,
    prepare_signal,
)
from mcsync.testing.media import (
    SHOOT_TIME_OF_DAY,
    creation_time,
    ffmpeg_available,
    run_ffmpeg,
    timecode_label,
    write_bwf,
    write_camera_clip,
)
from mcsync.testing.synthetic import make_scene, record

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")

AUDIO_TOL = 1e-3  # 1 ms


@pytest.fixture(scope="module")
def shoot(wedding_shoot):
    return wedding_shoot


@pytest.fixture(scope="module")
def scanned(shoot, tmp_path_factory):
    items, problems = scan_media([shoot.root])
    extract_audio(items, AnalysisCache(tmp_path_factory.mktemp("cache")))
    return items, problems


def by_rel(shoot, items):
    return {Path(it.path).relative_to(shoot.root).as_posix(): it for it in items}


def test_probe_reads_every_container(shoot, scanned):
    items, problems = scanned
    assert problems == []
    m = by_rel(shoot, items)
    assert set(m) == set(shoot.truth)
    cam_a = m["CAM_A/A001.MOV"].info
    assert cam_a.frame_rate == Fraction(24000, 1001)
    assert cam_a.timecode.text == timecode_label(30.0, Fraction(24000, 1001))
    assert cam_a.creation_time is not None
    cam_b = m["CAM_B/C0001.MP4"].info
    assert cam_b.timecode.drop_frame and cam_b.frame_rate == Fraction(30000, 1001)
    rec = m["ZOOM/230614_001.WAV"].info
    assert rec.is_audio_only and rec.timecode.source == "bwf"
    assert rec.timecode.seconds == pytest.approx(SHOOT_TIME_OF_DAY + 20.0)
    assert (rec.make, rec.model) == ("ZOOM", "F6")
    mts = m["CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS"].info
    assert mts.origin_s > 1.0  # MPEG-TS clocks do not start at zero
    assert mts.audio_start_s() == pytest.approx(-0.0053, abs=0.001)
    assert m["DRONE/DJI_0001.MP4"].audio_stream is None


def test_devices_and_chapters(shoot, scanned):
    m = by_rel(shoot, scanned[0])
    assert m["CAM_A/A001.MOV"].device.key == m["CAM_A/A002.MOV"].device.key
    assert m["CAM_A/A001.MOV"].device.key != m["CAM_B/C0001.MP4"].device.key
    assert m["ZOOM/230614_001.WAV"].device.kind == "recorder"
    assert m["DRONE/DJI_0001.MP4"].device.kind == "drone"
    assert m["CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS"].device.name.endswith("(CAM_C)")
    first, second = m["GOPRO/DCIM/100GOPRO/GH010042.MP4"], m["GOPRO/DCIM/100GOPRO/GH020042.MP4"]
    assert first.chapter[0] == second.chapter[0]
    assert (first.chapter[1], second.chapter[1]) == (0, 1)
    assert second.chapter_offset_s == pytest.approx(first.info.duration_s)


def test_full_pipeline_places_every_file(shoot, scanned):
    items, _ = scanned
    clips = build_clip_inputs(items, timecode_jam_synced=True)
    result = SyncEngine(SyncOptions(reference_clip_id=shoot.path(shoot.reference))).run(clips)
    placements = {Path(cid).relative_to(shoot.root).as_posix(): p for cid, p in result.placements.items()}

    for rel in ("CAM_A/A001.MOV", "CAM_A/A002.MOV", "CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS",
                "GOPRO/DCIM/100GOPRO/GH010042.MP4"):  # fmt: skip
        p = placements[rel]
        assert (p.status, p.method) == (PlacementStatus.SYNCED, PlacementMethod.AUDIO), rel
        assert p.start_s == pytest.approx(shoot.expected(rel), abs=AUDIO_TOL), rel

    # FFmpeg decodes this -itsoffset file including its AAC priming frame, so its
    # true in-file audio/video offset is ~0.7 ms from the intended 0.25 s.
    cam_b = placements["CAM_B/C0001.MP4"]
    assert cam_b.status == PlacementStatus.SYNCED
    assert cam_b.start_s == pytest.approx(shoot.expected("CAM_B/C0001.MP4"), abs=AUDIO_TOL)

    muted_chapter = placements["GOPRO/DCIM/100GOPRO/GH020042.MP4"]
    assert muted_chapter.method == PlacementMethod.CHAPTER
    assert muted_chapter.start_s == pytest.approx(shoot.expected("GOPRO/DCIM/100GOPRO/GH020042.MP4"), abs=2e-3)

    drone = placements["DRONE/DJI_0001.MP4"]
    assert drone.method == PlacementMethod.TIMECODE
    assert drone.start_s == pytest.approx(shoot.expected("DRONE/DJI_0001.MP4"), abs=1 / 30)


def test_without_jam_sync_the_drone_needs_review(shoot, scanned):
    """Per-device timecode cannot relate the drone to anything else."""
    clips = build_clip_inputs(scanned[0], timecode_jam_synced=False)
    result = SyncEngine(SyncOptions(reference_clip_id=shoot.path(shoot.reference))).run(clips)
    assert result.placements[shoot.path("DRONE/DJI_0001.MP4")].status == PlacementStatus.UNSYNCED
    assert result.placements[shoot.path("CAM_A/A002.MOV")].status == PlacementStatus.SYNCED


def test_clip_clocks(shoot, scanned):
    clips = {c.clip_id: c for c in build_clip_inputs(scanned[0], timecode_jam_synced=True)}
    cam_a = clips[shoot.path("CAM_A/A001.MOV")]
    sources = {c.source for c in cam_a.clocks}
    assert sources == {ClockSource.TIMECODE, ClockSource.CREATION_TIME}
    assert {c.domain for c in cam_a.clocks if c.source == ClockSource.TIMECODE} == {"tc:ntsc"}
    recorder = clips[shoot.path("ZOOM/230614_001.WAV")]
    assert [c.source for c in recorder.clocks] == [ClockSource.BWF]
    chapter = clips[shoot.path("GOPRO/DCIM/100GOPRO/GH020042.MP4")]
    assert any(c.source == ClockSource.CHAPTER and c.start_s > 0 for c in chapter.clocks)
    # Extraction pads delayed audio from the container start, so the audio starts with the clip.
    assert clips[shoot.path("CAM_B/C0001.MP4")].audio_start_s == 0.0
    assert clips[shoot.path("CAM_C/PRIVATE/AVCHD/BDMV/STREAM/00001.MTS")].audio_start_s == pytest.approx(
        -0.0053, abs=1e-3
    )


def test_rec_run_timecode_is_not_used_as_a_clock(tmp_path):
    """Takes 1 and 2 were 200 s apart, but rec-run timecode makes them contiguous."""
    scene = make_scene(400.0, kind="speech", rate=16000, seed=5)
    rate = Fraction(25)
    (tmp_path / "CAM").mkdir()
    write_camera_clip(tmp_path / "CAM" / "C0001.MP4", scene, 50.0, 40.0, timecode="01:00:00:00")
    write_camera_clip(tmp_path / "CAM" / "C0002.MP4", scene, 290.0, 40.0, timecode="01:00:40:00")
    items, _ = scan_media([tmp_path])
    clips = build_clip_inputs(items)
    assert all(not c.clocks or all(k.source != ClockSource.TIMECODE for k in c.clocks) for c in clips)
    assert timecode_label(0.0, rate)  # label helper works at integer rates too


def test_extraction_matches_in_memory_preparation(tmp_path):
    scene = make_scene(60.0, kind="speech", rate=16000, seed=9)
    path = tmp_path / "rec.wav"
    write_bwf(path, scene, 5.0, 50.0, rate=44100, channels=2, snr_db=20, seed=3)
    info = probe(path)
    signal = extract_to_cache(info, AnalysisCache(tmp_path / "c").entry(fingerprint(path), info.primary_audio.index))
    pcm = record(scene, start_s=5.0, duration_s=50.0, rate=44100, channels=2, snr_db=20, seed=3)
    reference = prepare_signal(pcm, 44100)
    assert isinstance(signal.samples, np.memmap)
    assert len(signal.samples) == len(reference.samples)
    assert signal.level_dbfs == pytest.approx(reference.level_dbfs, abs=0.1)
    assert estimate_offset(reference, signal).offset_s == pytest.approx(0.0, abs=20e-6)


def test_channel_selection(tmp_path):
    """A camera with the lavalier on channel 1 and its own microphone on channel 2."""
    scene_a, scene_b = make_scene(40.0, seed=1, rate=16000), make_scene(40.0, seed=2, rate=16000)
    left = record(scene_a, start_s=0.0, duration_s=30.0, rate=48000, seed=1)
    right = record(scene_b, start_s=0.0, duration_s=30.0, rate=48000, seed=2)
    path = tmp_path / "dual.wav"
    run_ffmpeg(["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "pipe:0", "-c:a", "pcm_s16le", str(path)],
               np.stack([left, right], axis=1))  # fmt: skip
    info = probe(path)
    cache = AnalysisCache(tmp_path / "c")
    fp = fingerprint(path)
    ch0 = extract_to_cache(info, cache.entry(fp, 0, channel=0), channel=0)
    ch1 = extract_to_cache(info, cache.entry(fp, 0, channel=1), channel=1)
    only_a = prepare_signal(left, 48000)
    assert estimate_offset(only_a, ch0).confidence > 0.9
    assert estimate_offset(only_a, ch1).offset_s is None or estimate_offset(only_a, ch1).confidence < 0.35
    with pytest.raises(ValueError):
        extract_to_cache(info, cache.entry(fp, 0, channel=5), channel=5)


def test_cancel_and_failure_leave_no_cache_entry(tmp_path):
    scene = make_scene(200.0, kind="speech", rate=16000, seed=4)
    path = tmp_path / "long.wav"
    write_bwf(path, scene, 0.0, 190.0, seed=1)
    info = probe(path)
    entry = AnalysisCache(tmp_path / "c").entry(fingerprint(path), 0)
    cancel = threading.Event()
    with pytest.raises(ExtractionCancelled):
        extract_to_cache(info, entry, cancel=cancel, progress=lambda f: cancel.set())
    assert not entry.exists()
    assert not any(entry.directory.parent.glob(".*tmp")) if entry.directory.parent.exists() else True


def test_truncated_file_extracts_what_is_there(tmp_path):
    """The last file on a card whose battery died mid-recording."""
    scene = make_scene(60.0, kind="speech", rate=16000, seed=4)
    whole = tmp_path / "whole.wav"
    write_bwf(whole, scene, 0.0, 50.0, seed=1)
    cut = tmp_path / "cut.wav"
    cut.write_bytes(whole.read_bytes()[: 44 + 48000 * 3 * 20])  # 20 s of 24-bit audio survived
    info = probe(cut)
    signal = extract_to_cache(info, AnalysisCache(tmp_path / "c").entry(fingerprint(cut), 0))
    assert signal.duration_s == pytest.approx(20.0, abs=0.1)
    assert not signal.is_silent()


def test_extraction_error_reports_ffmpeg_message(tmp_path):
    scene = make_scene(20.0, kind="speech", rate=16000, seed=4)
    path = tmp_path / "ok.wav"
    write_bwf(path, scene, 0.0, 10.0, seed=1)
    info = probe(path)
    path.unlink()
    with pytest.raises(ExtractionError):
        extract_to_cache(info, AnalysisCache(tmp_path / "c").entry("0" * 40, 0))
    assert ffmpeg_command(find_tools(), "in.mov", 1, 8000)[-1] == "pipe:1"


def test_scan_reports_non_media_and_skips_sidecars(tmp_path):
    (tmp_path / "notes.txt").write_text("shot list")
    (tmp_path / "C0001M01.XML").write_text("<x/>")
    (tmp_path / "garbage.mp4").write_bytes(b"\0" * 1000)
    scene = make_scene(20.0, kind="speech", rate=16000, seed=4)
    write_camera_clip(tmp_path / "clip.mov", scene, 0.0, 5.0, created=creation_time(0.0))
    items, problems = scan_media([tmp_path, tmp_path / "clip.mov"])
    assert [Path(it.path).name for it in items] == ["clip.mov"]  # scanned once despite being listed twice
    assert [Path(p.path).name for p in problems] == ["garbage.mp4"]
