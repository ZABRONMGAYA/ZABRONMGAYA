"""Pairwise offset estimation against synthetic recordings with known offsets."""

from __future__ import annotations

import numpy as np
import pytest

from mcsync.sync import Flag, MatchStatus, estimate_offset, prepare_signal
from mcsync.testing.synthetic import capture, record

REF_START = 60.0
SUBSAMPLE_TOL = 0.1e-3  # 100 µs; 1 frame at 60 fps is 16.7 ms
HARD_TOL = 0.5e-3


def _ref(scene, **kw):
    kw.setdefault("start_s", REF_START)
    kw.setdefault("duration_s", 300.0)
    kw.setdefault("seed", 1)
    return capture(scene, **kw)


@pytest.fixture(scope="module")
def speech_ref_48k(speech_scene):
    return _ref(speech_scene, rate=48000)


@pytest.fixture(scope="module")
def mixed_ref(mixed_scene):
    return _ref(mixed_scene, duration_s=600.0)


@pytest.mark.parametrize("true_offset", [0.0, 12.5, 37.123456, 101.00003, 240.0, -8.75])
def test_recovers_offset_with_subsample_accuracy(speech_scene, speech_ref_48k, true_offset):
    """Different devices, sample rates (48 kHz vs 44.1 kHz) and fractional offsets."""
    ref = speech_ref_48k
    tgt = capture(
        speech_scene,
        start_s=REF_START + true_offset,
        duration_s=45.0,
        rate=44100,
        snr_db=15,
        highpass_hz=250,
        lowpass_hz=6000,
        gain_db=-12,
        seed=2,
    )
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.offset_s == pytest.approx(true_offset, abs=SUBSAMPLE_TOL)
    assert est.confidence >= 0.9
    assert est.drift_ppm == 0.0


@pytest.mark.parametrize("seed", range(10))
def test_randomised_recording_conditions(mixed_scene, mixed_ref, seed):
    """Property-style sweep over offset, duration, SNR, reverb, EQ, gain and rate."""
    rng = np.random.default_rng(seed)
    ref = mixed_ref
    duration = float(rng.uniform(20.0, 120.0))
    offset = float(rng.uniform(-duration + 15.0, 600.0 - 15.0))
    tgt = capture(
        mixed_scene,
        start_s=REF_START + offset,
        duration_s=duration,
        rate=int(rng.choice([8000, 16000, 44100, 48000])),
        snr_db=float(rng.uniform(0.0, 30.0)),
        reverb_rt60_s=float(rng.uniform(0.0, 1.2)),
        direct_to_reverb_db=float(rng.uniform(-6.0, 6.0)),
        highpass_hz=float(rng.uniform(80.0, 400.0)),
        lowpass_hz=float(rng.uniform(3000.0, 3900.0)),
        gain_db=float(rng.uniform(-30.0, 0.0)),
        seed=100 + seed,
    )
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT, est
    assert est.offset_s == pytest.approx(offset, abs=HARD_TOL)


def test_hard_acoustics_camera_far_from_source(mixed_scene, mixed_ref):
    """-5 dB SNR, 1.5 s reverberation with more reverb than direct sound."""
    ref = mixed_ref
    tgt = capture(
        mixed_scene,
        start_s=REF_START + 150.3333,
        duration_s=60.0,
        snr_db=-5,
        reverb_rt60_s=1.5,
        direct_to_reverb_db=-6,
        highpass_hz=300,
        lowpass_hz=3000,
        gain_db=-25,
        seed=4,
    )
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.offset_s == pytest.approx(150.3333, abs=HARD_TOL)


def test_partial_overlap_at_the_end(speech_scene):
    ref = _ref(speech_scene)
    tgt = capture(speech_scene, start_s=REF_START + 270.25, duration_s=80.0, snr_db=10, seed=3)
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.offset_s == pytest.approx(270.25, abs=SUBSAMPLE_TOL)
    assert est.overlap_s == pytest.approx(29.75, abs=0.05)


def test_target_longer_than_reference(speech_scene):
    ref = capture(speech_scene, start_s=200.0, duration_s=40.0, seed=1)
    tgt = capture(speech_scene, start_s=100.5, duration_s=300.0, snr_db=10, seed=2)
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.offset_s == pytest.approx(-99.5, abs=SUBSAMPLE_TOL)


def test_is_antisymmetric(speech_scene):
    a = _ref(speech_scene)
    b = capture(speech_scene, start_s=REF_START + 42.4242, duration_s=60.0, snr_db=10, seed=2)
    forward, backward = estimate_offset(a, b), estimate_offset(b, a)
    assert forward.offset_s == pytest.approx(-backward.offset_s, abs=SUBSAMPLE_TOL)


def test_short_overlap_is_matched_but_flagged(speech_scene):
    ref = _ref(speech_scene)
    tgt = capture(speech_scene, start_s=REF_START + 294.0, duration_s=30.0, snr_db=10, seed=3)
    est = estimate_offset(ref, tgt)
    assert est.offset_s == pytest.approx(294.0, abs=SUBSAMPLE_TOL)
    assert Flag.SHORT_OVERLAP in est.flags
    assert est.status != MatchStatus.NO_MATCH


def test_clip_shorter_than_minimum_overlap_cannot_match(speech_scene):
    ref = _ref(speech_scene)
    tgt = capture(speech_scene, start_s=REF_START + 100.0, duration_s=2.0, seed=3)
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.NO_MATCH
    assert est.flags == (Flag.NO_OVERLAP,)


def test_unrelated_recordings_are_never_confident(unrelated_scenes):
    statuses = []
    for i, scene_a in enumerate(unrelated_scenes):
        ref = capture(scene_a, start_s=5.0, duration_s=140.0, seed=1)
        for j, scene_b in enumerate(unrelated_scenes):
            if i == j:
                continue
            tgt = capture(scene_b, start_s=20.0 + 7 * j, duration_s=[10.0, 30.0, 60.0][(i + j) % 3], snr_db=10, seed=2)
            est = estimate_offset(ref, tgt)
            assert est.status != MatchStatus.CONFIDENT, (i, j, est)
            statuses.append(est.status)
    assert statuses.count(MatchStatus.NO_MATCH) >= 0.8 * len(statuses)


def test_silent_recording_is_reported_as_silent(speech_scene):
    ref = _ref(speech_scene)
    quiet = record(speech_scene, start_s=REF_START + 50, duration_s=30.0, gain_db=-80, snr_db=None, seed=2)
    for tgt in (prepare_signal(quiet, 8000), prepare_signal(np.zeros(8000 * 30, dtype=np.float32), 8000)):
        est = estimate_offset(ref, tgt)
        assert est.status == MatchStatus.NO_MATCH
        assert est.flags == (Flag.SILENT,)


def test_exact_music_loop_is_flagged_ambiguous(loop_music_scene):
    """Sample-identical bars (a DJ loop) make every bar shift equally valid."""
    ref = capture(loop_music_scene, start_s=10.0, duration_s=200.0, seed=1)
    tgt = capture(loop_music_scene, start_s=100.1, duration_s=40.0, snr_db=15, seed=2)
    est = estimate_offset(ref, tgt)
    assert Flag.AMBIGUOUS in est.flags
    assert est.status == MatchStatus.UNCERTAIN
    assert any(a.n_inliers >= 3 for a in est.alternatives)
    # Every alternative is a whole number of bars (2 s at 120 bpm) away.
    for alt in est.alternatives:
        assert (alt.offset_s - est.offset_s) / 2.0 == pytest.approx(
            round((alt.offset_s - est.offset_s) / 2.0), abs=0.01
        )


def test_live_music_is_matched(live_music_scene):
    ref = capture(live_music_scene, start_s=10.0, duration_s=200.0, seed=1)
    tgt = capture(live_music_scene, start_s=100.1, duration_s=40.0, snr_db=15, reverb_rt60_s=0.8, seed=2)
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.offset_s == pytest.approx(90.1, abs=SUBSAMPLE_TOL)


def test_clock_drift_is_measured(speech_scene):
    """A camera clock running 60 ppm fast drifts 18 ms over 5 minutes."""
    ref = capture(speech_scene, start_s=20.0, duration_s=390.0, seed=1)
    tgt = capture(speech_scene, start_s=50.0, duration_s=300.0, clock_ppm=60.0, snr_db=10, seed=2)
    est = estimate_offset(ref, tgt)
    assert est.status == MatchStatus.CONFIDENT
    assert est.drift_ppm == pytest.approx(60.0, abs=2.0)
    assert Flag.DRIFT in est.flags
    # The offset is reported where it was measured: the middle of the overlap
    # (here the whole target), on the target's own clock.
    assert est.offset_time_s == pytest.approx(150.0, abs=0.01)
    assert est.offset_s == pytest.approx(30.0 - 60e-6 * 150.0, abs=HARD_TOL)
    assert est.drift_std_ppm < 1.0
    assert all(w.inlier for w in est.windows)


def test_search_window_restricts_the_result(mixed_scene, mixed_ref):
    ref = mixed_ref
    tgt = capture(mixed_scene, start_s=REF_START + 50.3333, duration_s=60.0, snr_db=0, reverb_rt60_s=1.0, seed=4)
    inside = estimate_offset(ref, tgt, search=(49.0, 52.0))
    assert inside.status == MatchStatus.CONFIDENT
    assert inside.offset_s == pytest.approx(50.3333, abs=HARD_TOL)
    outside = estimate_offset(ref, tgt, search=(10.0, 20.0))
    assert outside.status != MatchStatus.CONFIDENT
    impossible = estimate_offset(ref, tgt, search=(5000.0, 6000.0))
    assert impossible.flags == (Flag.NO_OVERLAP,)
    with pytest.raises(ValueError):
        estimate_offset(ref, tgt, search=(3.0, 1.0))


def test_window_measurements_describe_the_overlap(speech_scene):
    ref = _ref(speech_scene)
    tgt = capture(speech_scene, start_s=REF_START + 30.0, duration_s=120.0, snr_db=10, seed=2)
    est = estimate_offset(ref, tgt)
    assert est.n_windows == len(est.windows) >= 3
    assert all(0.0 <= w.time_s <= 120.0 for w in est.windows)
    assert all(w.lag_s == pytest.approx(30.0, abs=SUBSAMPLE_TOL) for w in est.windows)
    assert est.n_inliers == est.n_windows
    assert est.inlier_fraction == 1.0
    assert 0.0 < est.correlation <= 1.0
    assert est.std_error_s < SUBSAMPLE_TOL


def test_signals_must_share_the_analysis_rate(speech_scene):
    from mcsync.sync import SyncParams

    ref = _ref(speech_scene)
    other = capture(speech_scene, start_s=90.0, duration_s=20.0, params=SyncParams(analysis_rate=16000), rate=16000)
    with pytest.raises(ValueError):
        estimate_offset(ref, other)
