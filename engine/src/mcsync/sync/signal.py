"""Conversion of decoded audio into the engine's analysis signal."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
from scipy import signal as sps

from .features import log_energy_envelope
from .params import DEFAULT_PARAMS, SyncParams


@dataclass(eq=False)
class AnalysisSignal:
    """Mono, band-limited, RMS-normalised float32 audio at the analysis rate.

    ``samples`` may be a ``numpy.memmap`` of a cache file; nothing here
    requires the whole signal to be resident until it is correlated.
    """

    samples: np.ndarray
    rate: int
    #: In-band RMS level before normalisation, in dBFS (-inf for digital silence).
    level_dbfs: float
    _envelopes: dict[tuple, np.ndarray] = field(default_factory=dict, repr=False)

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.rate

    def is_silent(self, params: SyncParams = DEFAULT_PARAMS) -> bool:
        return self.level_dbfs < params.silence_floor_dbfs

    def envelope(self, params: SyncParams = DEFAULT_PARAMS) -> np.ndarray:
        """Coarse-stage feature, cached per parameter set."""
        key = (params.feature_rate, params.envelope_floor_db, params.envelope_detrend_s)
        env = self._envelopes.get(key)
        if env is None:
            env = log_energy_envelope(
                self.samples,
                self.rate,
                feature_rate=params.feature_rate,
                floor_db=params.envelope_floor_db,
                detrend_s=params.envelope_detrend_s,
            )
            self._envelopes[key] = env
        return env


def _to_float(samples: np.ndarray) -> np.ndarray:
    if np.issubdtype(samples.dtype, np.integer):
        info = np.iinfo(samples.dtype)
        scale = float(max(-info.min, info.max + 1))
        return samples.astype(np.float32) / np.float32(scale)
    return samples.astype(np.float32, copy=False)


def prepare_signal(
    samples: np.ndarray,
    sample_rate: int,
    params: SyncParams = DEFAULT_PARAMS,
) -> AnalysisSignal:
    """Downmix, resample, band-pass and normalise decoded audio.

    Args:
        samples: ``(frames,)`` or ``(frames, channels)`` PCM. Integer PCM is
            scaled to [-1, 1); float PCM is assumed to already be in that range.
        sample_rate: Rate of ``samples`` in Hz.
        params: Engine parameters (analysis rate and band).

    The band-pass is causal. That is deliberate: the same filter is applied to
    every recording, so its phase response cancels in the cross-spectrum
    ``H·A·conj(H·B) = |H|²·A·conj(B)`` and cannot bias the measured offset.
    """
    x = np.asarray(samples)
    if x.ndim == 2:
        x = _to_float(x).mean(axis=1, dtype=np.float32)
    elif x.ndim == 1:
        x = _to_float(x)
    else:
        raise ValueError("samples must be 1-D (frames,) or 2-D (frames, channels)")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    rate = params.analysis_rate
    if sample_rate != rate:
        ratio = Fraction(rate, int(sample_rate))
        x = sps.resample_poly(x, ratio.numerator, ratio.denominator).astype(np.float32)

    sos = sps.butter(4, [params.band_low_hz, params.band_high_hz], btype="bandpass", fs=rate, output="sos")
    y = sps.sosfilt(sos, x).astype(np.float32)

    rms = float(np.sqrt(np.mean(np.square(y, dtype=np.float64)))) if len(y) else 0.0
    if rms > 1e-12:
        y /= np.float32(rms)
        level = 20.0 * np.log10(rms)
    else:
        y[:] = 0.0
        level = float("-inf")
    return AnalysisSignal(samples=y, rate=rate, level_dbfs=level)
