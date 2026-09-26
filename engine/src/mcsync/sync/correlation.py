"""FFT cross-correlation, GCC-PHAT weighting and peak utilities.

Lag convention: ``c[k] = sum_n ref[n + k] * tgt[n]``. A peak at ``k`` means
``tgt[n] ≈ ref[n + k]``, i.e. the target starts ``k`` samples into the
reference (``k < 0``: the target started first).
"""

from __future__ import annotations

import numpy as np
from scipy import fft as sfft

_PHAT_FLOOR = 1e-12


def cross_correlation(
    ref: np.ndarray,
    tgt: np.ndarray,
    *,
    min_lag: int | None = None,
    max_lag: int | None = None,
    phat_beta: float = 0.0,
    band: tuple[float, float] | None = None,
    rate: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Linear cross-correlation over ``[min_lag, max_lag]`` via one real FFT.

    Args:
        ref, tgt: 1-D signals.
        min_lag, max_lag: Inclusive lag range; defaults to every lag with any overlap.
        phat_beta: Generalised cross-correlation weighting. The cross-spectrum is
            divided by ``|X|**phat_beta``: 0 is plain correlation, 1 is PHAT
            (phase only), which sharpens the peak and suppresses reverberation.
        band: ``(low_hz, high_hz)``; cross-spectrum bins outside are zeroed so
            PHAT does not amplify out-of-band noise. Requires ``rate``.

    Returns:
        ``(values, lags)`` with ``values[i]`` the correlation at ``lags[i]``.

    The FFT size is the smallest that avoids circular aliasing for the
    requested lags only, which roughly halves the work for narrow searches.
    Two float32 inputs are transformed in single precision (about twice as
    fast); anything else in double precision.
    """
    single = np.asarray(ref).dtype == np.float32 and np.asarray(tgt).dtype == np.float32
    dtype = np.float32 if single else np.float64
    ref = np.asarray(ref, dtype=dtype)
    tgt = np.asarray(tgt, dtype=dtype)
    len_r, len_t = len(ref), len(tgt)
    if len_r == 0 or len_t == 0:
        return np.zeros(0), np.zeros(0, dtype=np.int64)
    full_min, full_max = -(len_t - 1), len_r - 1
    lo = full_min if min_lag is None else max(int(min_lag), full_min)
    hi = full_max if max_lag is None else min(int(max_lag), full_max)
    if lo > hi:
        return np.zeros(0), np.zeros(0, dtype=np.int64)

    # Circular lag k aliases k ± n; keep both outside the linear support.
    n = sfft.next_fast_len(max(full_max - lo, hi - full_min) + 1, real=True)
    spec = sfft.rfft(ref, n) * np.conj(sfft.rfft(tgt, n))
    if band is not None:
        if rate is None:
            raise ValueError("band requires rate")
        freqs = sfft.rfftfreq(n, 1.0 / rate)
        spec[(freqs < band[0]) | (freqs > band[1])] = 0.0
    if phat_beta > 0.0:
        mag = np.abs(spec)
        floor = max(float(mag.max()) * _PHAT_FLOOR, float(np.finfo(dtype).tiny))
        spec /= np.power(np.maximum(mag, floor), phat_beta)
    corr = sfft.irfft(spec, n)
    lags = np.arange(lo, hi + 1, dtype=np.int64)
    return corr[lags % n], lags


def parabolic_peak(y: np.ndarray, i: int) -> tuple[float, float]:
    """Sub-sample peak position and height from a parabola through ``y[i-1:i+2]``."""
    if i <= 0 or i >= len(y) - 1:
        return float(i), float(y[i])
    ym, y0, yp = float(y[i - 1]), float(y[i]), float(y[i + 1])
    if not (np.isfinite(ym) and np.isfinite(yp)):
        return float(i), y0
    denom = ym - 2.0 * y0 + yp
    if denom >= 0.0:
        return float(i), y0
    delta = float(np.clip(0.5 * (ym - yp) / denom, -0.5, 0.5))
    return i + delta, y0 - 0.25 * (ym - yp) * delta


def find_top_peaks(y: np.ndarray, count: int, exclusion: int) -> list[int]:
    """Indices of up to ``count`` highest local maxima, at least ``exclusion`` apart.

    Only true local maxima qualify, so the shoulder of a broad main lobe just
    outside the exclusion zone is never reported as a separate peak. A plateau
    counts once, at its first sample.
    """
    if len(y) == 0 or count <= 0:
        return []
    work = np.array(y, dtype=np.float64)
    if len(work) >= 3:
        left = np.r_[True, work[1:] > work[:-1]]
        right = np.r_[work[:-1] >= work[1:], True]
        work[~(left & right)] = -np.inf
    peaks: list[int] = []
    for _ in range(count):
        i = int(np.argmax(work))
        if not np.isfinite(work[i]):
            break
        peaks.append(i)
        work[max(0, i - exclusion) : i + exclusion + 1] = -np.inf
    return peaks


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation of two equal-length signals (0 for constant input)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    x = x - x.mean()
    y = y - y.mean()
    denom = np.sqrt(np.dot(x, x) * np.dot(y, y))
    return float(np.dot(x, y) / denom) if denom > 0 else 0.0


def robust_z(values: np.ndarray) -> tuple[float, float]:
    """Median and MAD-based standard deviation (the null distribution of a correlogram)."""
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med))) * 1.4826
    if mad <= 0.0:
        mad = float(np.std(values)) or 1e-12
    return med, mad
