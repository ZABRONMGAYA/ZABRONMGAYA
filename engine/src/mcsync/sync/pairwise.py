"""Offset estimation between two recordings (coarse-to-fine).

1. **Coarse**: cross-correlate the 200 Hz log-energy envelopes of the complete
   recordings. Each lag's correlation is divided by ``sqrt(overlap)`` so its
   noise has the same spread at every lag, then expressed as a robust z-score
   (peak-to-sidelobe ratio, PSR). The top peaks become candidates. This costs a
   single FFT of a few million points even for multi-hour recordings.
2. **Fine**: for each strong candidate, measure the lag with GCC-PHAT on up to
   ``max_fine_windows`` short waveform windows spread over the overlap, each
   searched within a small radius around the candidate. Parabolic
   interpolation gives sub-sample resolution.
3. **Verify**: fit ``lag(t) = offset + slope·t`` to the window lags with an
   exhaustive RANSAC (all point pairs; n ≤ 12). Inliers agree to within
   ``inlier_tolerance_s``. The slope is the clock drift. The candidate with
   the most inliers wins; a runner-up with nearly as many makes the match
   ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .confidence import WEAK_CORRELATION, classify, match_confidence
from .correlation import cross_correlation, find_top_peaks, parabolic_peak, pearson, robust_z
from .params import DEFAULT_PARAMS, SyncParams
from .signal import AnalysisSignal
from .types import Candidate, Flag, MatchStatus, OffsetEstimate, WindowMeasurement

#: Drift below this is indistinguishable from measurement noise and irrelevant
#: in practice (0.5 ppm = 1.8 ms per hour).
_MIN_DRIFT = 0.5e-6
_MIN_SLOPE_STD = 0.05e-6
#: A runner-up verified with at least this share of the winner's inliers is a
#: competing explanation, i.e. the match is ambiguous.
_AMBIGUITY_RATIO = 0.8
_MIN_VERIFIED_INLIERS = 3
#: Stop verifying a candidate when this many windows yield fewer than
#: ``_EARLY_ABORT_MIN_INLIERS`` agreeing ones: a true match agrees almost everywhere.
_EARLY_ABORT_WINDOWS = 4
_EARLY_ABORT_MIN_INLIERS = 3


@dataclass(frozen=True)
class _CoarsePeak:
    offset_s: float
    psr: float


@dataclass(frozen=True)
class _LineFit:
    slope: float
    #: Standard error of the slope; inf when fewer than three inliers.
    slope_std: float
    intercept: float
    inliers: np.ndarray
    std_error_s: float


@dataclass(frozen=True)
class _FineResult:
    offset_s: float
    #: Target-local time at which ``offset_s`` holds (the overlap midpoint).
    offset_time_s: float
    slope: float
    slope_std: float
    std_error_s: float
    overlap_s: float
    n_valid: int
    n_inliers: int
    correlation: float
    windows: tuple[WindowMeasurement, ...]

    @property
    def inlier_fraction(self) -> float:
        return self.n_inliers / self.n_valid if self.n_valid else 0.0

    @property
    def rank(self) -> tuple[int, float, float]:
        return (self.n_inliers, self.inlier_fraction, self.correlation)


def estimate_offset(
    ref: AnalysisSignal,
    tgt: AnalysisSignal,
    params: SyncParams = DEFAULT_PARAMS,
    *,
    search: tuple[float, float] | None = None,
) -> OffsetEstimate:
    """Estimate ``start(tgt) - start(ref)`` in seconds.

    Args:
        ref, tgt: Signals prepared with the same ``params``.
        search: Optional ``(min_s, max_s)`` range the offset must lie in (for
            example from timecode, or around a manual placement to snap it).
    """
    if ref.rate != params.analysis_rate or tgt.rate != params.analysis_rate:
        raise ValueError("signals must be prepared at params.analysis_rate")
    if search is not None and search[0] > search[1]:
        raise ValueError("search window must be (min_s, max_s) with min_s <= max_s")
    if ref.is_silent(params) or tgt.is_silent(params):
        return _no_match((Flag.SILENT,))

    peaks = _coarse_peaks(ref, tgt, params, search)
    if not peaks:
        return _no_match((Flag.NO_OVERLAP,))
    best_psr = peaks[0].psr
    if best_psr < params.detection_psr:
        return _no_match(
            (Flag.NO_CORRELATION,),
            coarse_psr=best_psr,
            alternatives=tuple(Candidate(p.offset_s, p.psr) for p in peaks),
        )

    gate = max(params.detection_psr, params.candidate_ratio * best_psr)
    refined: list[tuple[_CoarsePeak, _FineResult]] = []
    for peak in peaks[: params.max_candidates]:
        if peak.psr < gate:
            break
        fine = _refine(ref, tgt, peak.offset_s, params)
        if fine is not None and fine.n_valid > 0:
            refined.append((peak, fine))
    if not refined:
        return _no_match((Flag.SILENT_OVERLAP,), coarse_psr=best_psr)

    refined.sort(key=lambda pf: pf[1].rank, reverse=True)
    chosen_peak, fine = refined[0]
    ambiguous = any(
        other.n_inliers >= _MIN_VERIFIED_INLIERS and other.n_inliers >= _AMBIGUITY_RATIO * fine.n_inliers
        for _, other in refined[1:]
    )
    others_psr = [p.psr for p in peaks if p is not chosen_peak]
    uniqueness = chosen_peak.psr / max(others_psr) if others_psr and max(others_psr) > 0 else float("inf")

    confidence = match_confidence(
        coarse_psr=chosen_peak.psr,
        n_inliers=fine.n_inliers,
        inlier_fraction=fine.inlier_fraction,
        overlap_s=fine.overlap_s,
        ambiguous=ambiguous,
        params=params,
        correlation=fine.correlation if fine.n_inliers >= _MIN_VERIFIED_INLIERS else None,
    )
    drift_ppm = -fine.slope * 1e6 if fine.slope else 0.0
    drift_std_ppm = fine.slope_std * 1e6

    flags: list[Flag] = []
    if ambiguous:
        flags.append(Flag.AMBIGUOUS)
    if fine.n_inliers >= _MIN_VERIFIED_INLIERS and fine.correlation < WEAK_CORRELATION:
        flags.append(Flag.WEAK_CORRELATION)
    if fine.n_valid < _MIN_VERIFIED_INLIERS:
        flags.append(Flag.UNVERIFIED)
    elif fine.inlier_fraction < 0.6:
        flags.append(Flag.INCONSISTENT_WINDOWS)
    if fine.overlap_s < params.short_overlap_s:
        flags.append(Flag.SHORT_OVERLAP)
    if abs(fine.slope) * fine.overlap_s > params.drift_warning_s:
        flags.append(Flag.DRIFT)

    refined_by_peak = {id(p): f for p, f in refined}
    alternatives = tuple(
        Candidate(
            offset_s=refined_by_peak[id(p)].offset_s if id(p) in refined_by_peak else p.offset_s,
            coarse_psr=p.psr,
            n_inliers=refined_by_peak[id(p)].n_inliers if id(p) in refined_by_peak else 0,
            inlier_fraction=refined_by_peak[id(p)].inlier_fraction if id(p) in refined_by_peak else 0.0,
        )
        for p in peaks
        if p is not chosen_peak
    )

    return OffsetEstimate(
        offset_s=fine.offset_s,
        confidence=confidence,
        status=classify(confidence, params),
        offset_time_s=fine.offset_time_s,
        drift_ppm=drift_ppm,
        drift_std_ppm=drift_std_ppm,
        std_error_s=fine.std_error_s,
        overlap_s=fine.overlap_s,
        coarse_psr=chosen_peak.psr,
        uniqueness=uniqueness,
        n_windows=fine.n_valid,
        inlier_fraction=fine.inlier_fraction,
        correlation=fine.correlation,
        flags=tuple(flags),
        windows=fine.windows,
        alternatives=alternatives,
    )


def _no_match(
    flags: tuple[Flag, ...],
    *,
    coarse_psr: float = 0.0,
    alternatives: tuple[Candidate, ...] = (),
) -> OffsetEstimate:
    return OffsetEstimate(
        offset_s=None,
        confidence=0.0,
        status=MatchStatus.NO_MATCH,
        coarse_psr=coarse_psr,
        flags=flags,
        alternatives=alternatives,
    )


# ---------------------------------------------------------------------------
# Coarse stage
# ---------------------------------------------------------------------------


def _coarse_peaks(
    ref: AnalysisSignal,
    tgt: AnalysisSignal,
    params: SyncParams,
    search: tuple[float, float] | None,
) -> list[_CoarsePeak]:
    fr = params.feature_rate
    env_r, env_t = ref.envelope(params), tgt.envelope(params)
    n_r, n_t = len(env_r), len(env_t)
    min_overlap = max(1, int(np.ceil(params.min_overlap_s * fr)))
    if min(n_r, n_t) < min_overlap:
        return []
    lag_min, lag_max = -(n_t - min_overlap), n_r - min_overlap

    corr, lags = cross_correlation(env_r, env_t, min_lag=lag_min, max_lag=lag_max)
    overlap = np.minimum(n_r, lags + n_t) - np.maximum(0, lags)
    z = corr / np.sqrt(overlap)
    # The null distribution is estimated over every admissible lag, even when a
    # search window restricts where the peak may be.
    median, spread = robust_z(z)
    psr = (z - median) / spread
    if search is not None:
        inside = (lags >= np.floor(search[0] * fr)) & (lags <= np.ceil(search[1] * fr))
        if not inside.any():
            return []
        psr = np.where(inside, psr, -np.inf)

    exclusion = max(1, int(round(params.peak_exclusion_s * fr)))
    peaks = []
    for i in find_top_peaks(psr, params.max_candidates + 1, exclusion):
        k, height = parabolic_peak(psr, i)
        peaks.append(_CoarsePeak(offset_s=(lags[0] + k) / fr, psr=height))
    return peaks


# ---------------------------------------------------------------------------
# Fine stage
# ---------------------------------------------------------------------------


def _padded_slice(x: np.ndarray, start: int, length: int) -> np.ndarray:
    out = np.zeros(length, dtype=np.float32)
    lo, hi = max(0, start), min(len(x), start + length)
    if hi > lo:
        out[lo - start : hi - start] = x[lo:hi]
    return out


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


def _spread_order(n: int) -> list[int]:
    """Visit order that covers the whole range early: both ends, middle, quarters, ..."""
    order = [0, n - 1] if n > 1 else list(range(n))
    seen = set(order)
    intervals = [(0, n - 1)]
    while intervals:
        nxt = []
        for lo, hi in intervals:
            if hi - lo >= 2:
                mid = (lo + hi) // 2
                if mid not in seen:
                    order.append(mid)
                    seen.add(mid)
                nxt += [(lo, mid), (mid, hi)]
        intervals = nxt
    return order


def _refine(ref: AnalysisSignal, tgt: AnalysisSignal, coarse_offset_s: float, params: SyncParams) -> _FineResult | None:
    fs = ref.rate
    a, b = ref.samples, tgt.samples
    lag0 = int(round(coarse_offset_s * fs))
    tb_start, tb_end = max(0, -lag0), min(len(b), len(a) - lag0)
    overlap = tb_end - tb_start
    if overlap <= 0:
        return None
    overlap_s = overlap / fs

    win = int(fs * min(params.fine_window_s, max(params.min_fine_window_s, overlap_s / 3.0)))
    win = max(1, min(win, overlap))
    n_win = int(min(params.max_fine_windows, max(1, overlap // win)))
    radius = int(np.ceil(fs * (params.fine_margin_s + params.max_drift_ppm * 1e-6 * overlap_s)))
    if n_win == 1:
        starts = np.array([tb_start + (overlap - win) // 2])
    else:
        starts = np.linspace(tb_start, tb_end - win, n_win).round().astype(np.int64)

    silence = 10.0 ** (params.window_silence_db / 20.0)
    band = (params.band_low_hz, params.band_high_hz)
    times, lags, rhos = [], [], []
    # Spread-out visiting order: a wrong candidate is abandoned after a few
    # windows, and those few already span the overlap.
    for w in _spread_order(len(starts)):
        tb = int(starts[w])
        seg_t = np.asarray(b[tb : tb + win], dtype=np.float32)
        seg_r = _padded_slice(a, tb + lag0 - radius, win + 2 * radius)
        if _rms(seg_t) < silence or _rms(seg_r[radius : radius + win]) < silence:
            continue
        corr, _ = cross_correlation(
            seg_r, seg_t, min_lag=0, max_lag=2 * radius, phat_beta=params.phat_beta, band=band, rate=fs
        )
        i = int(np.argmax(corr))
        k, _ = parabolic_peak(corr, i)
        times.append((tb + win / 2.0) / fs)
        lags.append((lag0 - radius + k) / fs)
        rhos.append(pearson(seg_r[i : i + win], seg_t))
        if len(lags) == _EARLY_ABORT_WINDOWS and len(starts) > _EARLY_ABORT_WINDOWS:
            probe = _fit_lag_line(np.array(times), np.array(lags), params, fs)
            if probe.inliers.sum() < _EARLY_ABORT_MIN_INLIERS:
                break

    t_mid = (tb_start + tb_end) / 2.0 / fs
    if not lags:
        return _FineResult(coarse_offset_s, t_mid, 0.0, float("inf"), float("inf"), overlap_s, 0, 0, 0.0, ())

    by_time = np.argsort(times)
    t, y, rho = np.array(times)[by_time], np.array(lags)[by_time], np.array(rhos)[by_time]
    fit = _fit_lag_line(t, y, params, fs)
    windows = tuple(
        WindowMeasurement(time_s=float(ti), lag_s=float(yi), correlation=float(ri), inlier=bool(ok))
        for ti, yi, ri, ok in zip(t, y, rho, fit.inliers, strict=True)
    )
    n_in = int(fit.inliers.sum())
    return _FineResult(
        offset_s=fit.intercept + fit.slope * t_mid,
        offset_time_s=t_mid,
        slope=fit.slope,
        slope_std=fit.slope_std,
        std_error_s=fit.std_error_s,
        overlap_s=overlap_s,
        n_valid=len(y),
        n_inliers=n_in,
        correlation=float(np.median(rho[fit.inliers])) if n_in else 0.0,
        windows=windows,
    )


def _fit_lag_line(t: np.ndarray, y: np.ndarray, params: SyncParams, fs: int) -> _LineFit:
    """Robust fit of ``lag = intercept + slope·t`` with drift-significance testing."""
    tol = params.inlier_tolerance_s
    max_slope = params.max_drift_ppm * 1e-6

    # Exhaustive RANSAC: every point as a constant model, every pair as a line.
    hypotheses = [(0.0, float(v)) for v in y]
    for i in range(len(y)):
        for j in range(i + 1, len(y)):
            if t[j] != t[i]:
                slope = (y[j] - y[i]) / (t[j] - t[i])
                if abs(slope) <= max_slope:
                    hypotheses.append((float(slope), float(y[i] - slope * t[i])))
    best_key, best = None, hypotheses[0]
    for slope, intercept in hypotheses:
        r = np.abs(y - (intercept + slope * t))
        ok = r <= tol
        key = (int(ok.sum()), -float(np.sum(r[ok] ** 2)))
        if best_key is None or key > best_key:
            best_key, best = key, (slope, intercept)
    inliers = np.abs(y - (best[1] + best[0] * t)) <= tol

    # Least-squares refit on the inliers; keep the slope only if significant.
    for _ in range(2):
        ti, yi = t[inliers], y[inliers]
        slope, intercept, slope_std = 0.0, float(np.mean(yi)), float("inf")
        if len(yi) >= 3 and np.ptp(ti) > 0:
            s, c = np.polyfit(ti, yi, 1)
            resid = yi - (c + s * ti)
            sxx = float(np.sum((ti - ti.mean()) ** 2))
            se_slope = float(np.sqrt(np.sum(resid**2) / (len(yi) - 2) / sxx))
            # Sub-sample interpolation limits how well a slope can be known.
            slope_std = max(se_slope, _MIN_SLOPE_STD)
            if abs(s) > max(3.0 * se_slope, _MIN_DRIFT) and abs(s) <= max_slope:
                slope, intercept = float(s), float(c)
        new_inliers = np.abs(y - (intercept + slope * t)) <= tol
        if not new_inliers.any() or np.array_equal(new_inliers, inliers):
            break
        inliers = new_inliers

    resid = y[inliers] - (intercept + slope * t[inliers])
    n_params = 2 if slope else 1
    floor = 0.05 / fs
    if len(resid) > n_params:
        std_error = float(np.sqrt(np.sum(resid**2) / (len(resid) - n_params)) / np.sqrt(len(resid)))
    else:
        std_error = 0.25 / fs
    return _LineFit(
        slope=slope, slope_std=slope_std, intercept=intercept, inliers=inliers, std_error_s=max(std_error, floor)
    )
