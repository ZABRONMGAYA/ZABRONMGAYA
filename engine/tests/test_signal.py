import numpy as np
import pytest

from mcsync.sync import SyncParams, estimate_offset, prepare_signal
from mcsync.sync.features import frame_energy, log_energy_envelope
from mcsync.testing.synthetic import record


def tone(freq, seconds, rate, amplitude):
    t = np.arange(int(seconds * rate)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_prepare_resamples_normalises_and_measures_level():
    x = tone(1000.0, 3.0, 48000, amplitude=0.1 * np.sqrt(2))  # -20 dBFS RMS, in band
    sig = prepare_signal(x, 48000)
    assert sig.rate == 8000
    assert len(sig.samples) == 3 * 8000
    assert sig.samples.dtype == np.float32
    assert sig.duration_s == pytest.approx(3.0)
    assert float(np.sqrt(np.mean(sig.samples.astype(np.float64) ** 2))) == pytest.approx(1.0, rel=1e-4)
    assert sig.level_dbfs == pytest.approx(-20.0, abs=0.2)
    assert not sig.is_silent()


def test_out_of_band_content_counts_as_silence():
    rumble = tone(40.0, 3.0, 8000, amplitude=0.05)  # -29 dBFS wind / handling rumble only
    assert prepare_signal(rumble, 8000).is_silent()


def test_digital_silence():
    sig = prepare_signal(np.zeros(16000, dtype=np.float32), 8000)
    assert sig.level_dbfs == float("-inf")
    assert sig.is_silent()
    assert not sig.samples.any()


def test_integer_pcm_and_channel_downmix():
    mono = tone(700.0, 2.0, 8000, amplitude=0.25)
    stereo_int16 = np.round(np.stack([mono, mono], axis=1) * 32767).astype(np.int16)
    a, b = prepare_signal(mono, 8000), prepare_signal(stereo_int16, 8000)
    assert b.level_dbfs == pytest.approx(a.level_dbfs, abs=0.01)
    np.testing.assert_allclose(a.samples, b.samples, atol=1e-3)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        prepare_signal(np.zeros((2, 2, 2)), 8000)
    with pytest.raises(ValueError):
        prepare_signal(np.zeros(100), 0)


def test_resampling_does_not_shift_timing(speech_scene):
    """The same audio at different input rates must align at zero offset."""
    kw = dict(start_s=40.0, duration_s=30.0, snr_db=None, seed=1)
    base = prepare_signal(record(speech_scene, rate=8000, **kw), 8000)
    for rate in (11025, 16000, 22050, 32000, 44100, 48000, 96000):
        other = prepare_signal(record(speech_scene, rate=rate, **kw), rate)
        est = estimate_offset(base, other)
        assert est.offset_s == pytest.approx(0.0, abs=20e-6), rate


def test_frame_energy_matches_direct_computation():
    x = np.random.default_rng(0).standard_normal(40 * 1000 + 17).astype(np.float32)
    energy = frame_energy(x, 40)
    assert len(energy) == 1000
    np.testing.assert_allclose(energy, (x[:40000].reshape(1000, 40) ** 2).mean(axis=1), rtol=1e-5)


def test_envelope_is_standardised_and_gain_invariant():
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(8000 * 20) * np.repeat(rng.uniform(0.05, 1.0, 200), 800)).astype(np.float32)
    env = log_energy_envelope(x, 8000, feature_rate=200, floor_db=-40.0, detrend_s=1.0)
    assert len(env) == 20 * 200
    assert env.mean() == pytest.approx(0.0, abs=1e-9)
    assert env.std() == pytest.approx(1.0, abs=1e-9)
    loud = log_energy_envelope(x * 30, 8000, feature_rate=200, floor_db=-40.0, detrend_s=1.0)
    np.testing.assert_allclose(env, loud, atol=1e-6)
    assert len(log_energy_envelope(np.zeros(10), 8000, feature_rate=200, floor_db=-40, detrend_s=1)) == 0
    assert not log_energy_envelope(np.zeros(800), 8000, feature_rate=200, floor_db=-40, detrend_s=1).any()


def test_envelope_is_cached_per_parameter_set():
    sig = prepare_signal(np.random.default_rng(0).standard_normal(8000 * 5).astype(np.float32), 8000)
    params = SyncParams()
    assert sig.envelope(params) is sig.envelope(params)
    other = SyncParams(feature_rate=100)
    assert len(sig.envelope(other)) == len(sig.envelope(params)) // 2


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(analysis_rate=8000, feature_rate=300),
        dict(band_low_hz=3000.0, band_high_hz=2000.0),
        dict(band_high_hz=5000.0),
        dict(phat_beta=1.5),
        dict(uncertain_threshold=0.8, confident_threshold=0.7),
        dict(max_candidates=0),
        dict(min_fine_window_s=20.0),
    ],
)
def test_invalid_params_are_rejected(kwargs):
    with pytest.raises(ValueError):
        SyncParams(**kwargs)


def test_memory_mapped_signals_cross_processes_as_file_references(tmp_path):
    import pickle

    from mcsync.sync import AnalysisSignal

    sig = prepare_signal(np.random.default_rng(3).standard_normal(8000 * 60).astype(np.float32), 8000)
    path = tmp_path / "signal.f32"
    sig.samples.astype("<f4").tofile(path)
    mapped = AnalysisSignal(np.memmap(path, dtype="<f4", mode="r"), 8000, sig.level_dbfs)
    blob = pickle.dumps(mapped)
    assert len(blob) < 2000  # a path, not 1.9 MB of samples
    first, second = pickle.loads(blob), pickle.loads(blob)
    np.testing.assert_array_equal(first.samples, sig.samples)
    assert first._envelopes is second._envelopes  # one envelope cache per file per process
    copied = pickle.loads(pickle.dumps(sig))  # in-memory signals travel by value
    np.testing.assert_array_equal(copied.samples, sig.samples)
    assert copied.level_dbfs == sig.level_dbfs
