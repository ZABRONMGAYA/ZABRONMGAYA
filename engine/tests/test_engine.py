"""End-to-end synchronisation of synthetic multicamera shoots."""

from __future__ import annotations

import threading
import time

import pytest

from mcsync.sync import (
    ClipInput,
    ClockReading,
    ClockSource,
    Flag,
    ManualCorrections,
    ManualOffset,
    MatchStatus,
    PlacementMethod,
    PlacementStatus,
    SyncCancelled,
    SyncEngine,
    SyncMode,
    SyncOptions,
)
from mcsync.testing.synthetic import capture
from mcsync.timecode import Timecode, parse_frame_rate

TOL = 0.5e-3


def make_clip(scene, cid, start, duration, *, rate=8000, device=None, clock=None, audio=True, seed=0, **kw):
    signal = capture(scene, start_s=start, duration_s=duration, rate=rate, seed=seed, **kw) if audio else None
    return ClipInput(cid, audio=signal, duration_s=None if audio else duration, device_id=device, clock=clock)


def camera_clock(device, scene_time, clock_error_s=0.0):
    """Container creation time on a camera whose clock is set ``clock_error_s`` wrong (1 s resolution)."""
    return ClockReading(
        float(int(36000.0 + scene_time + clock_error_s)), domain=device, source=ClockSource.CREATION_TIME
    )


# Scene times at which each device started recording.
WEDDING = {
    "recorder": 20.0,  # continuous external recorder (reference)
    "A1": 30.0,  # camera A, interrupted three times
    "A2": 200.5,
    "A3": 500.25,  # runs past the end of the recorder
    "B1": 60.1,  # camera B at 44.1 kHz
    "B2": 420.9,
    "B3": 820.0,  # only overlaps A3 and C1: must be placed transitively
    "C1": 850.0,  # phone, no metadata
    "drone": 300.0,  # no audio, no clock
}


@pytest.fixture(scope="module")
def wedding_clips(mixed_scene):
    s = mixed_scene
    cam = dict(snr_db=10, highpass_hz=300, reverb_rt60_s=0.8, direct_to_reverb_db=0)
    return [
        make_clip(s, "recorder", 20.0, 700.0, rate=16000, device="zoom", snr_db=30, seed=1),
        make_clip(s, "A1", 30.0, 150.0, device="camA", clock=camera_clock("camA", 30.0, 187), seed=2, **cam),
        make_clip(s, "A2", 200.5, 250.0, device="camA", clock=camera_clock("camA", 200.5, 187), seed=3, **cam),
        make_clip(s, "A3", 500.25, 400.0, device="camA", clock=camera_clock("camA", 500.25, 187), seed=4, **cam),
        make_clip(s, "B1", 60.1, 300.0, rate=44100, device="camB", snr_db=5, seed=5),
        make_clip(s, "B2", 420.9, 200.0, device="camB", snr_db=5, seed=6),
        make_clip(s, "B3", 820.0, 60.0, device="camB", snr_db=5, seed=7),
        make_clip(s, "C1", 850.0, 40.0, device="phone", snr_db=5, gain_db=-20, seed=8),
        make_clip(s, "drone", 300.0, 120.0, device="drone", audio=False),
    ]


@pytest.fixture(scope="module")
def wedding_result(wedding_clips):
    return SyncEngine(SyncOptions(reference_clip_id="recorder")).run(wedding_clips)


def test_every_clip_with_audio_is_synced_to_sub_millisecond(wedding_result):
    for cid, truth in WEDDING.items():
        placement = wedding_result.placements[cid]
        if cid == "drone":
            continue
        assert placement.status == PlacementStatus.SYNCED, placement
        assert placement.group == 0
        assert placement.start_s == pytest.approx(truth - WEDDING["recorder"], abs=TOL), cid


def test_methods_and_transitive_placement(wedding_result):
    p = wedding_result.placements
    assert p["recorder"].method == PlacementMethod.REFERENCE
    assert all(p[c].method == PlacementMethod.AUDIO for c in ("A1", "A2", "A3", "B1", "B2", "B3", "C1"))
    b3_edges = [m for m in wedding_result.matches if "B3" in (m.ref_id, m.tgt_id) and m.status == MatchStatus.CONFIDENT]
    assert {m.ref_id if m.tgt_id == "B3" else m.tgt_id for m in b3_edges} <= {"A3", "C1"}


def test_clip_without_audio_or_clock_stays_unsynced(wedding_result):
    drone = wedding_result.placements["drone"]
    assert drone.status == PlacementStatus.UNSYNCED
    assert drone.start_s is None
    assert Flag.NO_AUDIO in drone.flags
    assert wedding_result.needs_review() == ["drone"]


def test_same_device_and_clock_disjoint_pairs_are_skipped(wedding_clips, wedding_result):
    pairs = {frozenset((m.ref_id, m.tgt_id)) for m in wedding_result.matches}
    assert frozenset(("A1", "A2")) not in pairs  # same camera
    assert frozenset(("B1", "B3")) not in pairs
    assert len(pairs) == len(wedding_result.matches)
    audio_clips = [c for c in wedding_clips if c.audio is not None]
    assert len(pairs) < len(audio_clips) * (len(audio_clips) - 1) // 2


def test_timeline_starts_at_the_earliest_clip(wedding_result):
    timeline = wedding_result.timeline()
    assert min(timeline.values()) == 0.0
    assert timeline["recorder"] == pytest.approx(0.0, abs=TOL)
    assert timeline["C1"] == pytest.approx(830.0, abs=TOL)


def test_manual_corrections_resolve_without_reanalysis(wedding_clips, wedding_result):
    """The UI flow: the user drags B2 elsewhere, then rejects a match; only solve() runs."""
    engine = SyncEngine(SyncOptions(reference_clip_id="recorder"))
    corrections = ManualCorrections(offsets=[ManualOffset("B2", "recorder", 123.0)])
    started = time.perf_counter()
    moved = engine.solve(wedding_clips, wedding_result.matches, corrections)
    assert time.perf_counter() - started < 0.5
    b2 = moved.placements["B2"]
    assert (b2.start_s, b2.method, b2.status) == (123.0, PlacementMethod.MANUAL, PlacementStatus.SYNCED)
    assert Flag.CONFLICTING_MATCHES in b2.flags  # the audio disagreed; the user overrode it
    for cid in ("A2", "A3", "B1"):  # neighbours are neither moved nor flagged
        assert moved.placements[cid].start_s == pytest.approx(wedding_result.placements[cid].start_s, abs=1e-6)
        assert moved.placements[cid].status == PlacementStatus.SYNCED

    corrections = ManualCorrections()
    for m in wedding_result.matches:
        if "C1" in (m.ref_id, m.tgt_id):
            corrections.reject_pair(m.ref_id, m.tgt_id)
    orphaned = engine.solve(wedding_clips, wedding_result.matches, corrections)
    assert orphaned.placements["C1"].status == PlacementStatus.UNSYNCED
    assert orphaned.placements["B3"].status == PlacementStatus.SYNCED  # still reachable through A3


def test_snap_to_audio_near_a_rough_manual_position(wedding_clips):
    engine = SyncEngine()
    clips = {c.clip_id: c for c in wedding_clips}
    rough = 400.0  # dragged by hand; truth is 400.9 after the recorder
    snapped = engine.match_pair(clips["recorder"], clips["B2"], window=(rough - 2.0, rough + 2.0))
    assert snapped.status == MatchStatus.CONFIDENT
    assert snapped.offset_s == pytest.approx(400.9, abs=TOL)
    reverse = engine.match_pair(clips["B2"], clips["recorder"], window=(-rough - 2.0, -rough + 2.0))
    assert reverse.offset_s == pytest.approx(-400.9, abs=TOL)


def test_hybrid_timecode_prunes_pairs_and_audio_refines(mixed_scene):
    """Jam-synced timecode: frame-accurate positions, audio adds sub-frame precision."""
    rate = parse_frame_rate("25")

    def tc(scene_time):  # the timecode label of the frame being recorded at scene_time
        return ClockReading(float(Timecode.from_seconds(3600 + scene_time, rate).to_seconds(rate)), domain="jam")

    s = mixed_scene
    clips = [
        make_clip(s, "rec", 10.0, 400.0, device="rec", clock=tc(10.0), seed=1),
        make_clip(s, "A", 50.013, 100.0, device="A", clock=tc(50.013), snr_db=10, seed=2),
        make_clip(s, "B", 250.031, 100.0, device="B", clock=tc(250.031), snr_db=10, seed=3),
        make_clip(s, "drone", 120.0, 60.0, device="drone", clock=tc(120.0), audio=False),
    ]
    engine = SyncEngine(SyncOptions(reference_clip_id="rec"))
    result = engine.run(clips)
    pairs = {frozenset((m.ref_id, m.tgt_id)) for m in result.matches}
    assert frozenset(("A", "B")) not in pairs  # timecode says they never overlap
    assert all(m.search_window is not None for m in result.matches)
    assert result.placements["A"].start_s == pytest.approx(40.013, abs=TOL)
    assert result.placements["B"].start_s == pytest.approx(240.031, abs=TOL)
    drone = result.placements["drone"]
    assert drone.method == PlacementMethod.TIMECODE
    assert drone.status == PlacementStatus.SYNCED
    assert drone.start_s == pytest.approx(110.0, abs=0.04)  # one frame

    tc_only = SyncEngine(SyncOptions(mode=SyncMode.TIMECODE, reference_clip_id="rec"))
    assert tc_only.analyze(clips) == []
    by_tc = tc_only.run(clips)
    assert by_tc.placements["A"].start_s == pytest.approx(40.0, abs=1e-9)  # frame-quantised
    assert by_tc.placements["A"].method == PlacementMethod.TIMECODE


def test_wrong_timecode_falls_back_to_full_search(mixed_scene):
    s = mixed_scene
    clips = [
        make_clip(s, "rec", 10.0, 400.0, device="rec", clock=ClockReading(0.0, "jam"), seed=1),
        make_clip(s, "A", 50.0, 100.0, device="A", clock=ClockReading(40.0, "jam"), snr_db=10, seed=2),
        make_clip(s, "B", 150.0, 100.0, device="B", clock=ClockReading(200.0, "jam"), snr_db=10, seed=3),  # 60 s off
    ]
    result = SyncEngine(SyncOptions(reference_clip_id="rec")).run(clips)
    b = result.placements["B"]
    assert b.start_s == pytest.approx(140.0, abs=TOL)
    assert Flag.TIMECODE_DISAGREES in b.flags
    assert b.status == PlacementStatus.NEEDS_REVIEW
    rec_b = next(m for m in result.matches if {m.ref_id, m.tgt_id} == {"rec", "B"})
    assert Flag.CLOCK_MISMATCH in rec_b.all_flags
    assert result.placements["A"].status == PlacementStatus.SYNCED


def test_interrupted_clip_with_unusable_audio_is_placed_by_its_camera_clock(mixed_scene):
    s = mixed_scene
    clips = [
        make_clip(s, "rec", 10.0, 600.0, device="rec", seed=1),
        make_clip(s, "A1", 40.0, 100.0, device="camA", clock=camera_clock("camA", 40.0, -95), seed=2),
        # Lens cap on the mic / muted input: no usable audio.
        make_clip(s, "A2", 250.0, 90.0, device="camA", clock=camera_clock("camA", 250.0, -95), gain_db=-90, seed=3),
        make_clip(s, "A3", 400.0, 100.0, device="camA", clock=camera_clock("camA", 400.0, -95), seed=4),
    ]
    result = SyncEngine(SyncOptions(reference_clip_id="rec")).run(clips)
    a2 = result.placements["A2"]
    assert a2.method == PlacementMethod.METADATA
    assert a2.status == PlacementStatus.NEEDS_REVIEW
    assert a2.start_s == pytest.approx(240.0, abs=1.5)  # creation time has 1 s resolution
    silent = [m for m in result.matches if "A2" in (m.ref_id, m.tgt_id)]
    assert silent and all(Flag.SILENT in m.all_flags for m in silent)


def test_mixed_frame_rate_timecode(mixed_scene):
    """23.976 camera and 29.97 drop-frame camera jam-synced to one generator."""
    film, ntsc = parse_frame_rate("23.976"), parse_frame_rate("29.97")

    def tc(scene_time, rate, drop):
        label = Timecode.from_seconds(3600 + scene_time, rate, drop)
        assert (";" in str(label)) == drop
        return ClockReading(float(label.to_seconds(rate)), domain="jam", sigma_s=float(1 / rate))

    s = mixed_scene
    clips = [
        make_clip(s, "rec", 0.0, 300.0, device="rec", clock=tc(0.0, film, False), seed=1),
        make_clip(s, "A", 80.0, 60.0, device="A", clock=tc(80.0, film, False), audio=False),
        make_clip(s, "B", 150.0, 60.0, device="B", clock=tc(150.0, ntsc, True), audio=False),
    ]
    result = SyncEngine(SyncOptions(reference_clip_id="rec")).run(clips)
    assert result.placements["A"].start_s == pytest.approx(80.0, abs=float(1 / film))
    assert result.placements["B"].start_s == pytest.approx(150.0, abs=float(1 / ntsc))


def test_audio_start_offset_inside_the_container(mixed_scene):
    """Audio stream starting 21 ms after the video (AAC priming, edit lists)."""
    s = mixed_scene
    ref = make_clip(s, "rec", 10.0, 300.0, seed=1)
    cam_audio = capture(s, start_s=100.021, duration_s=60.0, snr_db=10, seed=2)
    cam = ClipInput("cam", audio=cam_audio, audio_start_s=0.021)
    assert cam.duration_s == pytest.approx(60.021)
    engine = SyncEngine()
    result = engine.run([ref, cam])
    assert result.placements["cam"].start_s == pytest.approx(90.0, abs=TOL)
    match = result.matches[0]
    assert match.offset_s == pytest.approx(90.0, abs=TOL)
    assert match.estimate.offset_s == pytest.approx(90.021, abs=TOL)  # audio start to audio start
    assert match.audio_shift_s == pytest.approx(0.021)
    for raw, clip_level in zip(match.estimate.alternatives, match.alternatives, strict=True):
        assert clip_level.offset_s == pytest.approx(raw.offset_s - 0.021)
    # Swapping the roles expresses everything from the other clip's side.
    swapped = engine.match_pair(cam, ref)
    assert swapped.offset_s == pytest.approx(-90.0, abs=TOL)
    assert swapped.audio_shift_s == pytest.approx(-0.021)


def test_excluded_clip_is_never_the_automatic_reference(mixed_scene):
    s = mixed_scene
    clips = [make_clip(s, "long", 50.0, 300.0, seed=1), make_clip(s, "a", 100.0, 60.0, seed=2)]
    clips.append(make_clip(s, "b", 120.0, 90.0, seed=3))
    corrections = ManualCorrections(excluded_clips={"long"})
    engine = SyncEngine()
    assert engine.reference_id(clips, corrections.excluded_clips) == "b"
    result = engine.run(clips, corrections)
    assert result.reference_id == "b"
    assert result.placements["a"].start_s == pytest.approx(-20.0, abs=TOL)
    assert result.placements["long"].status == PlacementStatus.UNSYNCED
    with pytest.raises(ValueError):
        SyncEngine(SyncOptions(reference_clip_id="long")).solve(clips, [], corrections)


def test_reference_defaults_to_the_longest_clip_with_audio(mixed_scene):
    s = mixed_scene
    clips = [
        make_clip(s, "short", 100.0, 60.0, seed=2),
        make_clip(s, "long", 50.0, 300.0, seed=1),
        make_clip(s, "silent-drone", 0.0, 900.0, audio=False),
    ]
    engine = SyncEngine()
    assert engine.reference_id(clips) == "long"
    result = engine.run(clips)
    assert result.reference_id == "long"
    assert result.placements["short"].start_s == pytest.approx(50.0, abs=TOL)


def test_progress_and_cancellation(wedding_clips):
    seen = []
    SyncEngine().analyze(wedding_clips[:4], progress=lambda f, msg: seen.append((f, msg)))
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert all(isinstance(msg, str) and msg for _, msg in seen)

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SyncCancelled):
        SyncEngine().analyze(wedding_clips, cancel=cancel)


def test_input_validation(wedding_clips, speech_scene):
    engine = SyncEngine()
    with pytest.raises(ValueError):
        engine.run([])
    with pytest.raises(ValueError):
        engine.run([wedding_clips[0], wedding_clips[0]])
    with pytest.raises(ValueError):
        SyncEngine(SyncOptions(reference_clip_id="missing")).run(wedding_clips[:2])
    from mcsync.sync import SyncParams

    wrong_rate = capture(speech_scene, start_s=0.0, duration_s=5.0, rate=16000, params=SyncParams(analysis_rate=16000))
    with pytest.raises(ValueError):
        engine.run([wedding_clips[0], ClipInput("x", audio=wrong_rate)])
    with pytest.raises(ValueError):
        ClipInput("no-duration")
    with pytest.raises(ValueError):
        engine.match_pair(wedding_clips[0], wedding_clips[-1])  # the drone has no audio


def test_parallel_matching_gives_the_serial_results(wedding_clips):
    from mcsync.sync.engine import create_match_pool

    engine = SyncEngine(SyncOptions(reference_clip_id="recorder"))
    pairs = engine.candidate_pairs(wedding_clips)
    serial = engine.match_pairs(pairs)
    seen = []
    with create_match_pool(2) as pool:
        parallel = engine.match_pairs(pairs, pool=pool, on_match=seen.append)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(SyncCancelled):
            engine.match_pairs(pairs, pool=pool, cancel=cancel)
    assert [(m.ref_id, m.tgt_id, m.offset_s) for m in parallel] == [(m.ref_id, m.tgt_id, m.offset_s) for m in serial]
    assert len(seen) == len(pairs)
