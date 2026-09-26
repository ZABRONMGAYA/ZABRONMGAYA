"""Synthetic acoustic scenes and device recordings with known ground truth.

A *scene* is the sound field of an event rendered once at a high rate. A
*recording* is what one device captured from it: its own start time, clock
error, microphone colouration, room reverberation, self-noise, gain and sample
rate. Because every recording is derived from the same scene, the true offset
between any two recordings is known exactly, including fractional-sample
offsets and clock drift.

Content types:

* ``speech``: voiced (harmonic, gliding pitch) and unvoiced (band-limited
  noise) syllables grouped into utterances separated by pauses;
* ``music``: drum pattern and chord pad at a fixed tempo. With
  ``exact_loop=True`` every bar is sample-identical, the worst case for any
  correlation-based synchroniser;
* ``ambience``: room tone plus sporadic claps, laughs and glass clinks;
* ``mixed``: all of the above, as at a wedding reception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from scipy import signal as sps

if TYPE_CHECKING:
    from mcsync.sync.signal import AnalysisSignal

SCENE_RMS = 0.1  # -20 dBFS


@dataclass(frozen=True)
class Scene:
    samples: np.ndarray  # float32 mono
    rate: int

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.rate


def _raised_cosine(n: int, attack: int, release: int) -> np.ndarray:
    env = np.ones(n)
    attack, release = min(attack, n // 2), min(release, n // 2)
    if attack:
        env[:attack] = 0.5 - 0.5 * np.cos(np.pi * np.arange(attack) / attack)
    if release:
        env[n - release :] = 0.5 + 0.5 * np.cos(np.pi * np.arange(release) / release)
    return env


def _bandpassed_noise(n: int, rate: int, lo: float, hi: float, rng: np.random.Generator) -> np.ndarray:
    hi = min(hi, 0.45 * rate)
    lo = min(lo, 0.5 * hi)  # keep the band valid at low scene rates
    sos = sps.butter(2, [lo, hi], btype="bandpass", fs=rate, output="sos")
    return sps.sosfilt(sos, rng.standard_normal(n))


def _add(out: np.ndarray, start: int, burst: np.ndarray) -> None:
    stop = min(len(out), start + len(burst))
    if stop > start >= 0:
        out[start:stop] += burst[: stop - start]


def _voiced(n: int, rate: int, rng: np.random.Generator) -> np.ndarray:
    """Source-filter vowel: gliding glottal pulse train through three formant resonators."""
    f0 = rng.uniform(95.0, 240.0)
    glide = rng.uniform(-0.25, 0.25)
    t = np.arange(n) / rate
    f_inst = f0 * (1.0 + glide * t / max(t[-1], 1e-9)) * (1.0 + 0.02 * np.sin(2 * np.pi * 5.5 * t))
    cycles = np.cumsum(f_inst) / rate
    excitation = np.zeros(n)
    excitation[1:][np.diff(np.floor(cycles)) > 0] = 1.0
    excitation += 0.02 * rng.standard_normal(n)  # breathiness
    out = np.zeros(n)
    for lo, hi in ((300, 900), (900, 2200), (2200, 3300)):
        f = rng.uniform(lo, hi)
        if f < 0.45 * rate:
            b, a = sps.iirpeak(f, Q=f / rng.uniform(80.0, 160.0), fs=rate)
            out += sps.lfilter(b, a, excitation)
    return out


def speech_like(duration_s: float, rate: int, rng: np.random.Generator) -> np.ndarray:
    n = int(round(duration_s * rate))
    out = np.zeros(n)
    t = rng.uniform(0.0, 0.5)
    while t < duration_s:
        utterance_end = t + rng.uniform(1.0, 4.0)
        while t < min(utterance_end, duration_s):
            syl = rng.uniform(0.08, 0.3)
            m = int(syl * rate)
            if m < 16:
                break
            burst = _voiced(m, rate, rng) if rng.random() < 0.75 else _bandpassed_noise(m, rate, 2000, 7000, rng)
            burst *= _raised_cosine(m, int(0.015 * rate), int(0.04 * rate)) * rng.uniform(0.2, 1.0)
            _add(out, int(t * rate), burst / (np.std(burst) + 1e-12))
            t += syl + rng.uniform(0.02, 0.12)
        t += rng.uniform(0.3, 1.5)
    return out


def _drum_bar(rate: int, bpm: float, rng: np.random.Generator, jitter_s: float) -> np.ndarray:
    beat = 60.0 / bpm
    n = int(round(4 * beat * rate))
    bar = np.zeros(n + int(0.3 * rate))

    def at(position_beats: float) -> int:
        return max(0, int((position_beats * beat + rng.uniform(-jitter_s, jitter_s)) * rate))

    kick_n = int(0.25 * rate)
    tk = np.arange(kick_n) / rate
    for b in (0, 2):
        f = rng.uniform(50.0, 60.0) if jitter_s else 55.0
        kick = np.sin(2 * np.pi * (f * tk + 1.8 * (1 - np.exp(-tk / 0.03)))) * np.exp(-tk / 0.12)
        _add(bar, at(b), kick * rng.uniform(0.8, 1.0))
    snare_n = int(0.18 * rate)
    for b in (1, 3):
        snare = _bandpassed_noise(snare_n, rate, 1500, 6000, rng) * np.exp(-np.arange(snare_n) / (0.05 * rate))
        _add(bar, at(b), 3.0 * snare / (np.std(snare) + 1e-12) * rng.uniform(0.6, 0.9))
    hat_n = int(0.05 * rate)
    for e in range(8):
        hat = _bandpassed_noise(hat_n, rate, 5000, 7500, rng) * np.exp(-np.arange(hat_n) / (0.01 * rate))
        _add(bar, at(e / 2), 1.5 * hat / (np.std(hat) + 1e-12) * rng.uniform(0.3, 0.5))
    return bar


_PROGRESSIONS = ((0, 7, 9, 5), (0, 5, 7, 5), (9, 5, 0, 7), (0, 9, 5, 7))


def _chord(root_hz: float, minor: bool) -> tuple[float, float, float]:
    third = 3 if minor else 4
    return (root_hz, root_hz * 2 ** (third / 12), root_hz * 2 ** (7 / 12))


def music_like(
    duration_s: float,
    rate: int,
    rng: np.random.Generator,
    *,
    bpm: float = 120.0,
    exact_loop: bool = False,
) -> np.ndarray:
    """Drums plus a chord pad.

    ``exact_loop=False`` models live players: every hit is displaced by up to
    ±8 ms, every chord note starts at a random phase, and the progression is
    chosen from the seed. ``exact_loop=True`` repeats one sample-identical bar,
    like a DJ loop.
    """
    n = int(round(duration_s * rate))
    bar_len = int(round(4 * 60.0 / bpm * rate))
    key_hz = 110.0 * 2 ** (int(rng.integers(12)) / 12)
    steps = _PROGRESSIONS[int(rng.integers(len(_PROGRESSIONS)))]
    chords = [_chord(key_hz * 2 ** (s / 12), s in (9, 4)) for s in steps]
    t = np.arange(bar_len) / rate
    fade = _raised_cosine(bar_len, int(0.05 * rate), int(0.05 * rate))

    def one_bar(i: int, jitter_s: float) -> np.ndarray:
        bar = _drum_bar(rate, bpm, rng, jitter_s)
        phases = rng.uniform(0, 2 * np.pi, 3) if jitter_s else np.zeros(3)
        pad = sum(np.sin(2 * np.pi * f * t + ph) for f, ph in zip(chords[i % len(chords)], phases, strict=True))
        bar[:bar_len] += 0.3 * pad * fade
        return bar

    out = np.zeros(n + 2 * bar_len)
    fixed = one_bar(0, 0.0) if exact_loop else None
    for i, start in enumerate(range(0, n, bar_len)):
        bar = fixed if fixed is not None else one_bar(i, 0.008)
        out[start : start + len(bar)] += bar
    return out[:n]


def ambience(duration_s: float, rate: int, rng: np.random.Generator, *, events_per_min: float = 12.0) -> np.ndarray:
    n = int(round(duration_s * rate))
    out = 0.05 * _bandpassed_noise(n, rate, 60, 4000, rng)
    for _ in range(rng.poisson(events_per_min * duration_s / 60.0)):
        start = int(rng.uniform(0, duration_s) * rate)
        kind = rng.integers(3)
        if kind == 0:  # clap
            m = int(0.06 * rate)
            burst = _bandpassed_noise(m, rate, 800, 5000, rng) * np.exp(-np.arange(m) / (0.01 * rate))
        elif kind == 1:  # laugh: train of short noisy bursts
            m = int(rng.uniform(0.6, 1.5) * rate)
            burst = _bandpassed_noise(m, rate, 300, 3000, rng)
            burst *= 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * rng.uniform(4, 7) * np.arange(m) / rate))
        else:  # glass clink
            m = int(0.4 * rate)
            burst = np.sin(2 * np.pi * rng.uniform(2500, 3500) * np.arange(m) / rate) * np.exp(
                -np.arange(m) / (0.08 * rate)
            )
        _add(out, start, rng.uniform(0.5, 2.0) * burst / (np.std(burst) + 1e-12))
    return out


def make_scene(
    duration_s: float,
    *,
    kind: str = "speech",
    rate: int = 32000,
    seed: int = 0,
    bpm: float = 120.0,
    exact_loop: bool = False,
) -> Scene:
    """Render a scene of ``duration_s`` seconds, normalised to -20 dBFS RMS."""
    rng = np.random.default_rng(seed)
    if kind == "speech":
        x = speech_like(duration_s, rate, rng) + 0.3 * ambience(duration_s, rate, rng, events_per_min=4)
    elif kind == "music":
        x = music_like(duration_s, rate, rng, bpm=bpm, exact_loop=exact_loop)
    elif kind == "ambience":
        x = ambience(duration_s, rate, rng)
    elif kind == "mixed":
        x = (
            speech_like(duration_s, rate, rng)
            + 0.4 * music_like(duration_s, rate, rng, bpm=bpm)
            + 0.5 * ambience(duration_s, rate, rng)
        )
    else:
        raise ValueError(f"unknown scene kind {kind!r}")
    x = x - x.mean()
    x *= SCENE_RMS / (np.std(x) + 1e-12)
    return Scene(samples=x.astype(np.float32), rate=rate)


def _reverb_ir(rate: int, rt60_s: float, direct_to_reverb_db: float, rng: np.random.Generator) -> np.ndarray:
    n = int(rt60_s * rate)
    tail = rng.standard_normal(n) * np.exp(-6.908 * np.arange(n) / n)  # -60 dB at rt60
    tail[: int(0.003 * rate)] = 0.0  # first reflections arrive after the direct path
    tail *= 10 ** (-direct_to_reverb_db / 20.0) / (np.sqrt(np.sum(tail**2)) + 1e-12)
    tail[0] = 1.0  # direct path defines the timing
    return tail


def record(
    scene: Scene,
    *,
    start_s: float,
    duration_s: float,
    rate: int = 8000,
    clock_ppm: float = 0.0,
    gain_db: float = 0.0,
    snr_db: float | None = 30.0,
    highpass_hz: float | None = None,
    lowpass_hz: float | None = None,
    reverb_rt60_s: float = 0.0,
    direct_to_reverb_db: float = 0.0,
    channels: int = 1,
    dtype: str = "float32",
    seed: int = 0,
) -> np.ndarray:
    """Capture ``scene`` as one device would.

    Args:
        start_s: Scene time of the recording's first sample (may be fractional).
        duration_s: Duration measured on the *device* clock.
        rate: Device sample rate.
        clock_ppm: Device clock error; positive runs fast (more samples per
            real second), which the engine reports as positive ``drift_ppm``.
        snr_db: Device self-noise relative to the captured signal; None = none.
        highpass_hz, lowpass_hz: Microphone colouration (zero-phase, so the
            ground-truth timing is not altered).
        reverb_rt60_s, direct_to_reverb_db: Room response (direct path at t=0).
        channels: Output channel count (channels differ only by their noise).
        dtype: ``float32`` or ``int16``.
    """
    rng = np.random.default_rng(seed)
    n = int(round(duration_s * rate))
    t_scene = start_s + np.arange(n) / (rate * (1.0 + clock_ppm * 1e-6))

    # Band-limit only the slice we need before interpolating onto device times.
    lo_idx = max(0, int(np.floor(t_scene[0] * scene.rate)) - 4096)
    hi_idx = min(len(scene.samples), int(np.ceil(t_scene[-1] * scene.rate)) + 4096)
    src = scene.samples[lo_idx:hi_idx].astype(np.float64)
    if rate < scene.rate and len(src) > 64:
        sos = sps.butter(8, 0.45 * rate, fs=scene.rate, output="sos")
        src = sps.sosfiltfilt(sos, src)
    x = np.interp(t_scene * scene.rate - lo_idx, np.arange(len(src)), src, left=0.0, right=0.0)

    if reverb_rt60_s > 0:
        ir = _reverb_ir(rate, reverb_rt60_s, direct_to_reverb_db, rng)
        x = sps.fftconvolve(x, ir)[:n]
    for cutoff, kind in ((highpass_hz, "highpass"), (lowpass_hz, "lowpass")):
        if cutoff is not None and len(x) > 64:
            x = sps.sosfiltfilt(sps.butter(2, cutoff, btype=kind, fs=rate, output="sos"), x)
    x *= 10 ** (gain_db / 20.0)

    sig_rms = float(np.std(x)) if len(x) else 0.0
    out = np.empty((n, channels))
    for ch in range(channels):
        noise = rng.standard_normal(n) * sig_rms * 10 ** (-snr_db / 20.0) if snr_db is not None else 0.0
        out[:, ch] = x + noise
    out = out[:, 0] if channels == 1 else out
    if dtype == "int16":
        return np.clip(np.round(out * 32767.0), -32768, 32767).astype(np.int16)
    return out.astype(np.float32)


def capture(scene: Scene, *, rate: int = 8000, params=None, **kwargs) -> AnalysisSignal:
    """Record ``scene`` (see :func:`record`) and prepare it for analysis."""
    from mcsync.sync.params import DEFAULT_PARAMS
    from mcsync.sync.signal import prepare_signal

    return prepare_signal(record(scene, rate=rate, **kwargs), rate, params or DEFAULT_PARAMS)
