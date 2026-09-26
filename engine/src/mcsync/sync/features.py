"""Coarse-stage features.

The coarse stage compares *loudness contours*, not waveforms. A camera 15 m
from the altar and a lavalier on the groom capture very different spectra and
reverberation, but their loudness rises and falls at the same instants. The
log-energy envelope below captures that while discarding gain, EQ and
noise-floor differences.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d

_BLOCK_FRAMES = 1 << 16


def frame_energy(x: np.ndarray, hop: int) -> np.ndarray:
    """Mean-square energy of consecutive non-overlapping frames of ``hop`` samples.

    Processed in blocks so a memory-mapped multi-hour signal is streamed rather
    than squared in one temporary array.
    """
    n_frames = len(x) // hop
    energy = np.empty(n_frames, dtype=np.float64)
    for start in range(0, n_frames, _BLOCK_FRAMES):
        stop = min(n_frames, start + _BLOCK_FRAMES)
        block = np.asarray(x[start * hop : stop * hop], dtype=np.float32).reshape(stop - start, hop)
        energy[start:stop] = np.einsum("ij,ij->i", block, block, dtype=np.float64) / hop
    return energy


def log_energy_envelope(
    x: np.ndarray,
    rate: int,
    *,
    feature_rate: int,
    floor_db: float,
    detrend_s: float,
) -> np.ndarray:
    """Detrended, standardised log-energy envelope at ``feature_rate`` Hz.

    Steps:
      1. frame energy with a hop of ``rate / feature_rate`` samples;
      2. ``10·log10(E + floor)`` where the floor sits ``floor_db`` below the
         mean frame energy, so quiet passages look alike on every device;
      3. subtract a ``detrend_s`` moving average (removes slow gain changes and
         automatic-gain-control pumping);
      4. zero mean, unit variance.
    """
    hop = rate // feature_rate
    energy = frame_energy(x, hop)
    if energy.size == 0:
        return np.zeros(0)
    mean_energy = float(energy.mean())
    if mean_energy <= 0.0:
        return np.zeros(energy.size)
    floor = mean_energy * 10.0 ** (floor_db / 10.0)
    log_e = 10.0 * np.log10(energy + floor)
    size = max(1, int(round(detrend_s * feature_rate)))
    env = log_e - uniform_filter1d(log_e, size=size, mode="nearest")
    env -= env.mean()
    std = env.std()
    if std > 1e-9:
        env /= std
    return env
