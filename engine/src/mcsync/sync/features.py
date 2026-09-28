"""Coarse-stage features.

The coarse stage compares *loudness contours*, not waveforms. A camera 15 m
from the altar and a lavalier on the groom capture very different spectra and
reverberation, but their loudness rises and falls at the same instants. The
log-energy envelope below captures that while discarding gain, EQ and
noise-floor differences.

A camera whose microphone mostly hears its own noise (a gimbal's motors, wind,
the operator's hands) has a loudness contour that follows the noise, not the
room. The *band envelope* looks for the room inside that noise: in each of a
dozen frequency bands it measures how far the level rises above that band's
own running level, in units of the band's usual variation, and averages the
bands. Steady noise (motor whine, hum, hiss) cancels in every band it occupies;
syllables, claps and notes still rise in theirs. On noise-dominated gimbal
clips this made the true offset stand out (median z-score 0.8 → 5.8).
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


def band_envelope(
    x: np.ndarray,
    rate: int,
    *,
    feature_rate: int,
    low_hz: float,
    high_hz: float,
    bands: int,
    detrend_s: float,
) -> np.ndarray:
    """Noise-robust coarse feature at ``feature_rate`` Hz (see the module docstring): per-band log-energy rises
    above a ``detrend_s`` running mean, divided by the band's median absolute deviation, clipped and averaged.

    Frames start every ``rate / feature_rate`` samples like :func:`log_energy_envelope` (same length), and are
    processed in blocks so multi-hour signals are streamed.
    """
    hop = rate // feature_rate
    n_frames = len(x) // hop
    if n_frames == 0:
        return np.zeros(0)
    nfft = 256 if rate <= 16000 else 512
    window = np.hanning(nfft).astype(np.float32)
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    edges = np.geomspace(low_hz, high_hz, bands + 1)
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(edges[:-1], edges[1:], strict=True)]
    keep = [m for m in masks if m.any()]
    if not keep:
        return np.zeros(n_frames)
    weights = np.stack([m / m.sum() for m in keep]).astype(np.float32)  # (bands, bins): band means
    log_bands = np.empty((len(keep), n_frames), dtype=np.float32)
    block = 1 << 14
    idx = np.arange(nfft)
    for start in range(0, n_frames, block):
        stop = min(n_frames, start + block)
        lo = start * hop
        seg = np.asarray(x[lo : min(len(x), (stop - 1) * hop + nfft)], dtype=np.float32)
        if len(seg) < (stop - start - 1) * hop + nfft:
            seg = np.pad(seg, (0, (stop - start - 1) * hop + nfft - len(seg)))
        frames = seg[(np.arange(stop - start) * hop)[:, None] + idx[None, :]] * window
        power = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32) ** 2
        log_bands[:, start:stop] = np.log(power @ weights.T + 1e-12).T
    size = max(1, int(round(detrend_s * feature_rate)))
    dev = log_bands - uniform_filter1d(log_bands, size=size, axis=1, mode="nearest")
    med = np.median(dev, axis=1, keepdims=True)
    mad = np.median(np.abs(dev - med), axis=1, keepdims=True) * 1.4826 + 1e-6
    env = np.clip((dev - med) / mad, -3.0, 6.0).mean(axis=0).astype(np.float64)
    env -= env.mean()
    std = env.std()
    if std > 1e-9:
        env /= std
    return env
