"""Waveform overviews for the timeline.

Min/max peak pairs at several zoom levels, built while audio is extracted and
stored as small int8 files next to the analysis signal. The UI reads the level
closest to its zoom (bins of 64 samples at 8 kHz = 8 ms, up to 65 536 samples =
8.2 s) instead of touching the audio itself.

Values are μ-law companded (μ = 255) to 8 bits, so quiet camera audio stays
visible next to a loud recorder track. Decode with :func:`decode_peaks`.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

PEAK_LEVELS: tuple[int, ...] = (64, 256, 1024, 4096, 16384, 65536)
_MU = 255.0


def _mulaw(x: np.ndarray) -> np.ndarray:
    y = np.sign(x) * np.log1p(_MU * np.minimum(np.abs(x), 1.0)) / np.log1p(_MU)
    return np.clip(np.round(y * 127.0), -127, 127).astype(np.int8)


def decode_peaks(peaks: np.ndarray) -> np.ndarray:
    """int8 μ-law peaks → linear amplitude in [-1, 1]."""
    y = peaks.astype(np.float32) / 127.0
    return np.sign(y) * np.expm1(np.abs(y) * np.log1p(_MU)) / _MU


class PeakBuilder:
    """Streaming min/max accumulation at the finest level."""

    def __init__(self) -> None:
        self._rest = np.zeros(0, dtype=np.float32)
        self._mins: list[np.ndarray] = []
        self._maxs: list[np.ndarray] = []

    def feed(self, x: np.ndarray) -> None:
        base = PEAK_LEVELS[0]
        data = np.concatenate([self._rest, np.asarray(x, dtype=np.float32)])
        n = len(data) // base * base
        if n:
            frames = data[:n].reshape(-1, base)
            self._mins.append(frames.min(axis=1))
            self._maxs.append(frames.max(axis=1))
        self._rest = data[n:]

    def finish(self) -> dict[int, np.ndarray]:
        """``{samples_per_bin: int8 array of shape (bins, 2)}`` for every level."""
        mins, maxs = list(self._mins), list(self._maxs)
        if len(self._rest):
            mins.append(np.array([self._rest.min()], dtype=np.float32))
            maxs.append(np.array([self._rest.max()], dtype=np.float32))
        lo = np.concatenate(mins) if mins else np.zeros(0, dtype=np.float32)
        hi = np.concatenate(maxs) if maxs else np.zeros(0, dtype=np.float32)
        levels: dict[int, np.ndarray] = {}
        for k, spb in enumerate(PEAK_LEVELS):
            if k:
                factor = spb // PEAK_LEVELS[k - 1]
                pad = (-len(lo)) % factor
                if pad:  # extend the last bin; min/max are unaffected
                    lo, hi = np.r_[lo, np.repeat(lo[-1:], pad)], np.r_[hi, np.repeat(hi[-1:], pad)]
                lo, hi = lo.reshape(-1, factor).min(axis=1), hi.reshape(-1, factor).max(axis=1)
            levels[spb] = np.stack([_mulaw(lo), _mulaw(hi)], axis=1) if len(lo) else np.zeros((0, 2), np.int8)
        return levels


def peaks_path(directory: Path, samples_per_bin: int) -> Path:
    return directory / f"peaks_{samples_per_bin}.i8"


def write_peaks(directory: Path, levels: dict[int, np.ndarray]) -> None:
    for spb, arr in levels.items():
        arr.astype(np.int8).tofile(peaks_path(directory, spb))


def read_peaks(directory: str | Path, samples_per_bin: int, start: int = 0, stop: int | None = None) -> np.ndarray:
    """Bins ``[start, stop)`` of one level as an int8 ``(n, 2)`` array of (min, max)."""
    if samples_per_bin not in PEAK_LEVELS:
        raise ValueError(f"samples_per_bin must be one of {PEAK_LEVELS}")
    path = peaks_path(Path(directory), samples_per_bin)
    if path.stat().st_size == 0:
        return np.zeros((0, 2), np.int8)
    data = np.memmap(path, dtype=np.int8, mode="r").reshape(-1, 2)
    return np.array(data[start:stop])
