import numpy as np
import pytest
from scipy import fft as sfft
from scipy import signal as sps

from mcsync.sync.correlation import cross_correlation, find_top_peaks, parabolic_peak, pearson, robust_z


def brute_force(ref, tgt, lag):
    return sum(ref[n + lag] * tgt[n] for n in range(len(tgt)) if 0 <= n + lag < len(ref))


def fractional_delay(x, delay):
    """Delay by a non-integer number of samples via a linear-phase FFT shift."""
    n = len(x)
    freqs = sfft.rfftfreq(n)
    return sfft.irfft(sfft.rfft(x) * np.exp(-2j * np.pi * freqs * delay), n)


@pytest.fixture
def noise():
    return np.random.default_rng(0).standard_normal(4000)


def test_full_correlation_matches_definition(noise):
    ref, tgt = noise[:50], noise[10:40] * 0.5 + 0.1
    values, lags = cross_correlation(ref, tgt)
    assert lags[0] == -(len(tgt) - 1) and lags[-1] == len(ref) - 1
    for lag, v in zip(lags, values, strict=True):
        assert v == pytest.approx(brute_force(ref, tgt, lag), abs=1e-9)


def test_restricted_lag_range_matches_full_range(noise):
    ref, tgt = noise, noise[1000:1700]
    full_v, full_l = cross_correlation(ref, tgt)
    part_v, part_l = cross_correlation(ref, tgt, min_lag=900, max_lag=1100)
    assert list(part_l) == list(range(900, 1101))
    np.testing.assert_allclose(part_v, full_v[np.isin(full_l, part_l)], atol=1e-9)
    empty_v, empty_l = cross_correlation(ref, tgt, min_lag=10_000, max_lag=20_000)
    assert len(empty_v) == len(empty_l) == 0


@pytest.mark.parametrize("start", [0, 123, 3000])
def test_peak_is_where_the_target_starts_in_the_reference(noise, start):
    values, lags = cross_correlation(noise, noise[start : start + 500])
    assert lags[np.argmax(values)] == start


def test_negative_lag_when_target_starts_first(noise):
    ref, tgt = noise[200:1200], noise[:600]
    values, lags = cross_correlation(ref, tgt)
    assert lags[np.argmax(values)] == -200


def test_phat_finds_the_lag_through_strong_colouration(noise):
    b, a = sps.butter(2, [0.02, 0.08], btype="bandpass")
    coloured = sps.filtfilt(b, a, noise)  # zero-phase: colours the spectrum, keeps the timing
    ref, tgt = noise, coloured[700:2700]
    for beta in (0.0, 0.8, 1.0):
        values, lags = cross_correlation(ref, tgt, phat_beta=beta)
        assert abs(lags[np.argmax(values)] - 700) <= 3
    sharpness = {
        beta: np.max(v) / np.std(v)
        for beta in (0.0, 0.8, 1.0)
        for v in [cross_correlation(ref, tgt, phat_beta=beta)[0]]
    }
    # Partial whitening (the engine default) sharpens the peak. Full PHAT
    # over-whitens: bins the colouration emptied become pure noise at unit weight.
    assert sharpness[0.8] > sharpness[0.0] > sharpness[1.0]


def test_band_requires_rate(noise):
    with pytest.raises(ValueError):
        cross_correlation(noise, noise, band=(100.0, 1000.0))


@pytest.mark.parametrize("delay", [0.1, 0.25, 0.5, 0.73])
def test_subsample_delay_with_parabolic_interpolation(delay):
    x = sps.lfilter(*sps.butter(4, 0.4), np.random.default_rng(1).standard_normal(16000))
    shifted = fractional_delay(x, delay)
    values, lags = cross_correlation(x, shifted[2000:10000], min_lag=1990, max_lag=2010, phat_beta=0.8)
    i = int(np.argmax(values))
    k, _ = parabolic_peak(values, i)
    # shifted[n] = x[n - delay], so the target starts `2000 - delay` samples into x.
    assert lags[0] + k == pytest.approx(2000 - delay, abs=0.08)


def test_parabolic_peak_exact_on_a_parabola_and_safe_at_edges():
    x = np.arange(10, dtype=float)
    y = -((x - 4.3) ** 2)
    assert parabolic_peak(y, 4)[0] == pytest.approx(4.3)
    assert parabolic_peak(y, 4)[1] == pytest.approx(0.0)
    assert parabolic_peak(y, 0) == (0.0, y[0])
    assert parabolic_peak(y, 9) == (9.0, y[9])
    flat = np.ones(5)
    assert parabolic_peak(flat, 2) == (2.0, 1.0)
    assert parabolic_peak(np.array([-np.inf, 1.0, 0.5]), 1) == (1.0, 1.0)


def test_find_top_peaks_respects_exclusion_and_local_maxima():
    y = np.zeros(100)
    y[20], y[21], y[22] = 5.0, 4.9, 4.8  # broad peak: only index 20 counts
    y[50] = 3.0
    y[53] = 2.5  # within exclusion of 50
    y[80] = 2.0
    y[90:95] = 1.0  # plateau: one peak, at its first sample
    assert find_top_peaks(y, 3, exclusion=5) == [20, 50, 80]
    many = find_top_peaks(y, 50, exclusion=5)
    assert many[:4] == [20, 50, 80, 90]
    assert not {21, 22, 53, 91, 92, 93, 94} & set(many)
    assert find_top_peaks(np.array([]), 3, 1) == []


def test_pearson_and_robust_z():
    x = np.arange(100, dtype=float)
    assert pearson(x, 2 * x + 1) == pytest.approx(1.0)
    assert pearson(x, -x) == pytest.approx(-1.0)
    assert pearson(x, np.ones(100)) == 0.0
    values = np.random.default_rng(2).standard_normal(100_000)
    values[:10] = 1000.0  # outliers do not move a robust estimate
    median, spread = robust_z(values)
    assert median == pytest.approx(0.0, abs=0.02)
    assert spread == pytest.approx(1.0, abs=0.02)
