"""Conversion of decoded audio into the engine's analysis signal."""

from __future__ import annotations

import threading
from collections import OrderedDict
from fractions import Fraction

import numpy as np
from scipy import signal as sps

from .features import log_energy_envelope
from .params import DEFAULT_PARAMS, SyncParams

#: Memory maps of cache files, shared by every signal of this process and bounded: each open map holds a file
#: descriptor, and a project with thousands of clips must never hold thousands of them.
_SHARED_MAPS: OrderedDict[tuple, tuple[np.ndarray, dict]] = OrderedDict()
_SHARED_MAPS_MAX = 64
_MAPS_LOCK = threading.Lock()


def _shared_map(source: tuple) -> tuple[np.ndarray, dict]:
    """The process-wide map of a cache file and its envelope cache (least recently used ones are closed)."""
    with _MAPS_LOCK:
        shared = _SHARED_MAPS.get(source)
        if shared is not None:
            _SHARED_MAPS.move_to_end(source)
            return shared
        filename, dtype, shape, offset = source
        if shape[0] == 0:
            samples: np.ndarray = np.zeros(0, dtype=np.float32)
        else:
            samples = np.memmap(filename, dtype=dtype, mode="r", shape=shape, offset=offset)
        shared = (samples, {})
        _SHARED_MAPS[source] = shared
        while len(_SHARED_MAPS) > _SHARED_MAPS_MAX:
            _SHARED_MAPS.popitem(last=False)
        return shared


def _source_of(samples: np.ndarray) -> tuple | None:
    if isinstance(samples, np.memmap) and samples.filename is not None:
        return (str(samples.filename), samples.dtype.str, samples.shape, samples.offset)
    return None


class AnalysisSignal:
    """Mono, band-limited, RMS-normalised float32 audio at the analysis rate.

    Either holds its ``samples`` or names a cache file (``source``: path, dtype, shape, offset) that is mapped
    only while the samples are in use, through a small process-wide pool of maps. Nothing here requires the whole
    signal to be resident until it is correlated.
    """

    def __init__(
        self,
        samples: np.ndarray | None,
        rate: int,
        level_dbfs: float,
        *,
        source: tuple | None = None,
    ) -> None:
        if samples is None and source is None:
            raise ValueError("a signal needs samples or a source file")
        self._samples = samples
        self._source = source if samples is None else None
        self._length = len(samples) if samples is not None else int(source[2][0])  # type: ignore[index]
        self.rate = rate
        #: In-band RMS level before normalisation, in dBFS (-inf for digital silence).
        self.level_dbfs = level_dbfs
        self._envelopes: dict[tuple, np.ndarray] = {}

    def __repr__(self) -> str:
        where = self._source[0] if self._source else "memory"
        return f"AnalysisSignal({self._length} samples at {self.rate} Hz, {self.level_dbfs:.1f} dBFS, {where})"

    @property
    def samples(self) -> np.ndarray:
        if self._samples is not None:
            return self._samples
        return _shared_map(self._source)[0]  # type: ignore[arg-type]

    @property
    def source(self) -> tuple | None:
        """The cache file behind a lazily mapped signal."""
        return self._source

    @property
    def n_samples(self) -> int:
        return self._length

    @property
    def duration_s(self) -> float:
        return self._length / self.rate

    def is_silent(self, params: SyncParams = DEFAULT_PARAMS) -> bool:
        return self.level_dbfs < params.silence_floor_dbfs

    def __getstate__(self) -> dict:
        # A file-backed signal crosses to worker processes as its file path only: every worker maps the same cache
        # file instead of receiving a copy.
        state = {"rate": self.rate, "level_dbfs": self.level_dbfs}
        source = self._source or (_source_of(self._samples) if self._samples is not None else None)
        if source is not None:
            state["memmap"] = source
        else:
            state["samples"] = np.asarray(self._samples)
            state["envelopes"] = self._envelopes
        return state

    def __setstate__(self, state: dict) -> None:
        self.rate = state["rate"]
        self.level_dbfs = state["level_dbfs"]
        if "memmap" in state:
            self._samples, self._source = None, state["memmap"]
            self._length = int(self._source[2][0])  # type: ignore[index]
            self._envelopes = {}
        else:
            self._samples, self._source = state["samples"], None
            self._length = len(self._samples)
            self._envelopes = state["envelopes"]

    def envelope(self, params: SyncParams = DEFAULT_PARAMS) -> np.ndarray:
        """Coarse-stage feature, cached per parameter set (per file for file-backed signals)."""
        key = (params.feature_rate, params.envelope_floor_db, params.envelope_detrend_s)
        if self._source is not None:
            samples, cache = _shared_map(self._source)
        else:
            samples, cache = self.samples, self._envelopes
        env = cache.get(key)
        if env is None:
            env = log_energy_envelope(
                samples,
                self.rate,
                feature_rate=params.feature_rate,
                floor_db=params.envelope_floor_db,
                detrend_s=params.envelope_detrend_s,
            )
            cache[key] = env
        return env


def _to_float(samples: np.ndarray) -> np.ndarray:
    if np.issubdtype(samples.dtype, np.integer):
        info = np.iinfo(samples.dtype)
        scale = float(max(-info.min, info.max + 1))
        return samples.astype(np.float32) / np.float32(scale)
    return samples.astype(np.float32, copy=False)


def band_filter(params: SyncParams = DEFAULT_PARAMS) -> np.ndarray:
    """Second-order sections of the analysis band-pass (shared by the streaming extractor)."""
    return sps.butter(
        4, [params.band_low_hz, params.band_high_hz], btype="bandpass", fs=params.analysis_rate, output="sos"
    )


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

    y = sps.sosfilt(band_filter(params), x).astype(np.float32)

    rms = float(np.sqrt(np.mean(np.square(y, dtype=np.float64)))) if len(y) else 0.0
    if rms > 1e-12:
        y /= np.float32(rms)
        level = 20.0 * np.log10(rms)
    else:
        y[:] = 0.0
        level = float("-inf")
    return AnalysisSignal(samples=y, rate=rate, level_dbfs=level)
