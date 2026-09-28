"""Visual evidence for AI sync: two cameras that watch the same event see the same changes of light (a photographer's
flash, stage lights, a door opening) at the same moment, even when they film different things.

Each video is reduced to its mean brightness ten times a second (cached next to its audio analysis), and two
cameras are compared by correlating their brightness *changes*, so exposure, framing and colour do not matter.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from mcsync.media.tools import FFmpegTools, subprocess_flags

RATE = 10.0  # brightness samples per second
_W, _H = 32, 18
_VERSION = 1


class VisualCancelled(Exception):
    pass


def brightness_file(cache_base: Path, fingerprint: str) -> Path:
    return cache_base / fingerprint[:2] / fingerprint / f"lum{int(RATE)}-v{_VERSION}" / "lum.f32"


def brightness(
    tools: FFmpegTools,
    path: str,
    cache_base: Path,
    fingerprint: str,
    cancel: threading.Event | None = None,
    progress: Callable[[float], None] | None = None,
    duration_s: float | None = None,
) -> np.ndarray:
    """Mean brightness (0–255) of the first video stream, ``RATE`` times a second, from the cache or decoded."""
    out = brightness_file(cache_base, fingerprint)
    if out.is_file():
        return np.fromfile(out, dtype="<f4")
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn",
           "-vf", f"fps={RATE:g},scale={_W}:{_H}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            **subprocess_flags())  # fmt: skip
    assert proc.stdout is not None
    frame = _W * _H
    values: list[float] = []
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise VisualCancelled()
            buf = proc.stdout.read(frame * 64)
            if not buf:
                break
            usable = len(buf) // frame * frame
            if usable:
                values.extend(np.frombuffer(buf[:usable], dtype=np.uint8).reshape(-1, frame).mean(axis=1).tolist())
            if progress is not None and duration_s:
                progress(min(1.0, len(values) / RATE / duration_s))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    lum = np.asarray(values, dtype="<f4")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    lum.tofile(tmp)
    (out.parent / "lum.json").write_text(json.dumps({"rate": RATE, "frames": len(lum), "source": path}))
    tmp.replace(out)
    return lum


def changes(lum: np.ndarray) -> np.ndarray:
    """Brightness changes, robustly normalised (a flash stands out whatever the scene's exposure)."""
    if len(lum) < 3:
        return np.zeros(0, dtype=np.float32)
    d = np.diff(lum.astype(np.float64))
    scale = np.median(np.abs(d - np.median(d))) * 1.4826 + 0.25
    return np.clip(d / scale, -20.0, 20.0).astype(np.float32)


_BURST_S = 0.3  # a flash lights up and fades: one event, at its start


def events(lum: np.ndarray, threshold: float = 6.0) -> list[float]:
    """Times (s) of sudden brightness changes: flashes, lights switched, cuts in the scene."""
    c = np.abs(changes(lum))
    idx = np.flatnonzero((c >= threshold) & (c >= np.roll(c, 1)) & (c >= np.roll(c, -1)))
    out: list[float] = []
    for i in idx:
        t = float(i + 1) / RATE
        if not out or t - out[-1] > _BURST_S + 1e-6:
            out.append(t)
    return out


@dataclass(frozen=True)
class VisualMatch:
    lag_s: float  # time in ``other`` = time in ``target`` + lag
    score: float  # normalised correlation of the brightness changes, 0–1
    ratio: float  # how much the best lag stands out from the next best (1 = not at all)
    shared_events: list[float]  # target times of brightness events seen by both


def correlate(target: np.ndarray, other: np.ndarray, lag_range: tuple[float, float]) -> VisualMatch | None:
    """The lag (within ``lag_range``) at which the two cameras' brightness changes agree best."""
    a, b = changes(target), changes(other)
    if len(a) < RATE * 3 or len(b) < RATE * 3:
        return None
    lo, hi = int(np.floor(lag_range[0] * RATE)), int(np.ceil(lag_range[1] * RATE))
    n = len(a) + len(b)
    size = 1 << (n - 1).bit_length()
    # corr[k] = sum_t a[t] * b[t + k]
    spec = np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size))
    full = np.fft.irfft(spec, size)
    lags = np.arange(-len(a) + 1, len(b))
    corr = np.concatenate([full[size - len(a) + 1 :], full[: len(b)]])
    keep = (lags >= lo) & (lags <= hi)
    if not keep.any():
        return None
    lags, corr = lags[keep], corr[keep]
    # Normalise by the energy of the overlapping parts.
    ea = np.cumsum(np.concatenate([[0.0], a.astype(np.float64) ** 2]))
    eb = np.cumsum(np.concatenate([[0.0], b.astype(np.float64) ** 2]))
    start_a = np.clip(-lags, 0, len(a))
    end_a = np.clip(len(b) - lags, 0, len(a))
    overlap = end_a - start_a
    b0, b1 = np.clip(start_a + lags, 0, len(b)), np.clip(end_a + lags, 0, len(b))
    energy = np.sqrt((ea[end_a] - ea[start_a]) * (eb[b1] - eb[b0]))
    valid = overlap >= RATE * 3
    # The lag is where the most changes agree (the raw correlation: a short overlap with one coincidence does not
    # outweigh a long one where every flash lines up); its score is how well they agree there.
    raw = np.where(valid, corr, 0.0)
    k = int(np.argmax(raw))
    if raw[k] <= 0 or energy[k] <= 0:
        return None
    best = float(corr[k] / energy[k])
    far = np.abs(lags - lags[k]) > RATE * 1.0
    second = float(raw[far].max()) if far.any() else 0.0
    lag = int(lags[k])
    ta, tb = events(target), set(np.round(np.asarray(events(other)) * RATE).astype(int).tolist())
    shared = [t for t in ta if any(abs(int(round(t * RATE)) + lag - e) <= 1 for e in tb)]
    return VisualMatch(lag / RATE, min(1.0, best), float(raw[k]) / max(second, float(raw[k]) * 1e-3), shared)
