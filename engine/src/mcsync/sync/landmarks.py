"""Audio landmarks: find which recordings share sound, and roughly where, without comparing every pair.

A production with thousands of clips has millions of possible pairs; only a few thousand of them actually overlap.
Landmark fingerprints (the technique behind song recognition) find those few directly:

1. **Peaks.** Each recording's spectrogram (64 ms frames every 32 ms, inside the analysis band) is reduced to its
   strongest local maxima, at most ``PEAKS_PER_SECOND`` per second. Peaks survive different microphones, levels,
   noise and room sound far better than the waveform does.
2. **Hashes.** Every peak is paired with the next few peaks close to it in time and frequency; each pair becomes a
   21-bit hash ``(f1, f2 - f1, t2 - t1)`` stamped with the time of the first peak.
3. **Index.** Every clip's hashes go into one inverted index on disk (sorted hash values, and for each the clip and
   time it came from), built in buckets so memory stays small however large the production.
4. **Votes.** To find what overlaps a clip, look up its hashes: each hit from another clip votes for the time
   difference between the two. Recordings of the same moment pile their votes on one difference; everything else
   scatters. The strongest differences become candidates, which the full matcher then verifies in a narrow window.

Hashes are 21 bits, so unrelated clips collide by chance; those collisions spread thinly over every time difference
and are measured (``background``) rather than assumed away. Hashes that occur very often (silence, hum, tones) are
skipped at query time.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import fft as sfft
from scipy.ndimage import maximum_filter

from .params import DEFAULT_PARAMS, SyncParams

LANDMARK_VERSION = 1
N_FFT = 512
HOP = 256
PEAKS_PER_SECOND = 12
#: Peak neighbourhood: a peak is the largest value within ±5 frames (160 ms) and ±10 bins (160 Hz at 8 kHz).
NEIGHBOURHOOD = (11, 21)
#: Peaks weaker than the block median by less than this are ignored (dB).
PEAK_MIN_PROMINENCE_DB = 10.0
FAN_OUT = 5
MAX_DT = 63  # frames (2 s)
MAX_DF = 63  # bins
_BLOCK_FRAMES = 4096
_T_MASK = np.uint64(0xFFFFFFFF)


def frame_seconds(rate: int) -> float:
    return HOP / rate


def _band_bins(params: SyncParams) -> tuple[int, int]:
    width = params.analysis_rate / N_FFT
    lo = max(1, int(np.ceil(params.band_low_hz / width)))
    hi = min(N_FFT // 2, int(params.band_high_hz / width))
    if hi - lo >= 256:
        hi = lo + 255  # f1 is stored in 8 bits
    return lo, hi


def _block_peaks(x: np.ndarray, first_frame: int, n_frames: int, margin: int, lo: int, hi: int) -> np.ndarray:
    """Peaks ``(frame, bin, level_db)`` of frames ``first_frame .. first_frame + n_frames`` (margins for context)."""
    start = max(0, first_frame - margin)
    stop = first_frame + n_frames + margin
    seg = np.asarray(x[start * HOP : (stop - 1) * HOP + N_FFT], dtype=np.float32)
    count = 1 + (len(seg) - N_FFT) // HOP if len(seg) >= N_FFT else 0
    if count <= 0:
        return np.zeros((0, 3))
    frames = np.lib.stride_tricks.as_strided(seg, shape=(count, N_FFT), strides=(HOP * 4, 4))
    spec = sfft.rfft(frames * np.hanning(N_FFT).astype(np.float32), axis=1)[:, lo : hi + 1]
    power = spec.real**2 + spec.imag**2
    level = 10.0 * np.log10(power + 1e-10)
    local_max = level == maximum_filter(level, size=NEIGHBOURHOOD, mode="constant", cval=-np.inf)
    floor = np.median(level) + PEAK_MIN_PROMINENCE_DB
    t, f = np.nonzero(local_max & (level > floor) & (power > 1e-6))
    t_abs = t + start
    inside = (t_abs >= first_frame) & (t_abs < first_frame + n_frames)
    return np.column_stack([t_abs[inside], f[inside] + lo, level[t[inside], f[inside]]])


def compute_landmarks(samples: np.ndarray, rate: int, params: SyncParams = DEFAULT_PARAMS) -> np.ndarray:
    """Landmark hashes of a prepared analysis signal: ``uint64`` values ``hash << 32 | frame``, in frame order.

    Streams the (possibly memory-mapped) signal in blocks of about two minutes.
    """
    if rate != params.analysis_rate:
        raise ValueError("landmarks need a signal at the analysis rate")
    n_frames = 1 + (len(samples) - N_FFT) // HOP if len(samples) >= N_FFT else 0
    lo, hi = _band_bins(params)
    margin = NEIGHBOURHOOD[0] // 2
    blocks = [_block_peaks(samples, s, min(_BLOCK_FRAMES, n_frames - s), margin, lo, hi)
              for s in range(0, n_frames, _BLOCK_FRAMES)]  # fmt: skip
    peaks = np.concatenate(blocks) if blocks else np.zeros((0, 3))
    if len(peaks) == 0:
        return np.zeros(0, dtype=np.uint64)

    # Keep the strongest PEAKS_PER_SECOND of every second.
    per_second = max(1, int(round(rate / HOP)))
    second = (peaks[:, 0] // per_second).astype(np.int64)
    order = np.lexsort((-peaks[:, 2], second))
    peaks, second = peaks[order], second[order]
    starts = np.r_[0, np.flatnonzero(np.diff(second)) + 1]
    rank = np.arange(len(second)) - np.repeat(starts, np.diff(np.r_[starts, len(second)]))
    peaks = peaks[rank < PEAKS_PER_SECOND]
    peaks = peaks[np.lexsort((peaks[:, 1], peaks[:, 0]))]
    t = peaks[:, 0].astype(np.int64)
    f = peaks[:, 1].astype(np.int64)

    # Pair every anchor with its next FAN_OUT neighbours inside the target zone. A 2 s zone touches at most three
    # whole seconds and two partial ones, so the next 4 * PEAKS_PER_SECOND peaks cover it. Anchors are processed in
    # chunks so an hours-long recording needs a few MB.
    n = len(t)
    look = min(n - 1, 4 * PEAKS_PER_SECOND)
    if look <= 0:
        return np.zeros(0, dtype=np.uint64)
    k = np.arange(1, look + 1)
    out = []
    for first in range(0, n, 8192):
        a = np.arange(first, min(n, first + 8192))
        j = a[:, None] + k[None, :]
        valid = j < n
        j = np.minimum(j, n - 1)
        dt = t[j] - t[a, None]
        df = f[j] - f[a, None]
        valid &= (dt >= 1) & (dt <= MAX_DT) & (np.abs(df) <= MAX_DF)
        valid &= np.cumsum(valid, axis=1) <= FAN_OUT
        row, pos = np.nonzero(valid)
        anchor = a[row]
        hashes = ((f[anchor] - lo) << 13) | ((df[row, pos] + 64) << 6) | dt[row, pos]
        out.append((hashes.astype(np.uint64) << np.uint64(32)) | t[anchor].astype(np.uint64))
    return np.concatenate(out)


def landmark_file(directory: Path) -> Path:
    return directory / f"landmarks-v{LANDMARK_VERSION}.npy"


def ensure_landmarks(directory: Path, samples: np.ndarray, rate: int, params: SyncParams = DEFAULT_PARAMS) -> Path:
    """Compute a cache entry's landmarks once; later calls return the stored file."""
    path = landmark_file(directory)
    if not path.is_file():
        tmp = directory / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp.npy"
        np.save(tmp, compute_landmarks(samples, rate, params))
        os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

_BUCKET_SHIFT = 15  # 21-bit hashes → 64 buckets


@dataclass(frozen=True)
class IndexClip:
    key: str  # the caller's clip id
    landmarks: str  # path of the clip's landmark file
    device: str | None
    n_frames: int


@dataclass(frozen=True)
class Candidate:
    """``start(query) - start(other)`` in audio time is about ``offset_s``."""

    other: str
    offset_s: float
    votes: int
    background: float  # votes expected at one offset by chance
    total_votes: int  # every vote between the two clips, at any offset


class LandmarkIndex:
    """Inverted index of every clip's landmark hashes, on disk (memory-mapped when queried)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        meta = json.loads((directory / "meta.json").read_text())
        self.meta = meta
        self.clips = [IndexClip(**c) for c in meta["clips"]]
        self.position = {c.key: k for k, c in enumerate(self.clips)}
        self.rate = int(meta["rate"])
        devices: dict[str | None, int] = {}
        self._device = np.array([devices.setdefault(c.device, len(devices)) if c.device is not None else -1 - k
                                 for k, c in enumerate(self.clips)], dtype=np.int64)  # fmt: skip
        self._frames = np.array([c.n_frames for c in self.clips], dtype=np.int64)
        n = int(meta["entries"])
        if n:
            self.hashes = np.memmap(directory / "hashes.u32", dtype="<u4", mode="r", shape=(n,))
            self.postings = np.memmap(directory / "postings.u64", dtype="<u8", mode="r", shape=(n,))
        else:
            self.hashes = np.zeros(0, dtype="<u4")
            self.postings = np.zeros(0, dtype="<u8")

    @property
    def entries(self) -> int:
        return len(self.hashes)

    @staticmethod
    def signature(clips: list[IndexClip]) -> str:
        text = json.dumps([LANDMARK_VERSION, [(c.key, c.landmarks, c.device, c.n_frames) for c in clips]])
        return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:16]

    @classmethod
    def build(cls, root: Path, clips: list[IndexClip], rate: int) -> LandmarkIndex:
        """Build (or reuse) the index of ``clips`` under ``root``. Memory use is one clip's landmarks or one bucket
        (1/64 of the index), whichever is larger."""
        signature = cls.signature(clips)
        final = root / f"index-{signature}"
        if (final / "meta.json").is_file():
            return cls(final)
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / f".index-{signature}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        tmp.mkdir()
        try:
            n_buckets = 1 << (21 - _BUCKET_SHIFT)
            counts = np.zeros(n_buckets, dtype=np.int64)
            for c in clips:
                h = (np.load(c.landmarks, mmap_mode="r") >> np.uint64(32 + _BUCKET_SHIFT)).astype(np.int64)
                counts += np.bincount(h, minlength=n_buckets)
            total = int(counts.sum())
            starts = np.r_[0, np.cumsum(counts)[:-1]]
            if total:
                hashes = np.memmap(tmp / "hashes.u32", dtype="<u4", mode="w+", shape=(total,))
                postings = np.memmap(tmp / "postings.u64", dtype="<u8", mode="w+", shape=(total,))
                cursor = starts.copy()
                for k, c in enumerate(clips):
                    lm = np.load(c.landmarks)
                    if len(lm) == 0:
                        continue
                    h = (lm >> np.uint64(32)).astype(np.uint32)
                    post = (np.uint64(k) << np.uint64(32)) | (lm & _T_MASK)
                    bucket = (h >> _BUCKET_SHIFT).astype(np.int64)
                    order = np.argsort(bucket, kind="stable")
                    h, post, bucket = h[order], post[order], bucket[order]
                    present, first, size = np.unique(bucket, return_index=True, return_counts=True)
                    for b, i, s in zip(present, first, size, strict=True):
                        dst = cursor[b]
                        hashes[dst : dst + s] = h[i : i + s]
                        postings[dst : dst + s] = post[i : i + s]
                        cursor[b] += s
                for b in range(n_buckets):
                    lo, hi = int(starts[b]), int(starts[b] + counts[b])
                    if hi - lo > 1:
                        order = np.argsort(hashes[lo:hi], kind="stable")
                        hashes[lo:hi] = hashes[lo:hi][order]
                        postings[lo:hi] = postings[lo:hi][order]
                hashes.flush()
                postings.flush()
                del hashes, postings
            meta = {
                "version": LANDMARK_VERSION,
                "rate": rate,
                "entries": total,
                "clips": [c.__dict__ for c in clips],
            }
            (tmp / "meta.json").write_text(json.dumps(meta))
            try:
                tmp.rename(final)
            except OSError:
                if not (final / "meta.json").is_file():
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return cls(final)

    def remove_others(self) -> None:
        """Delete older indexes next to this one."""
        for d in self.directory.parent.glob("index-*"):
            if d != self.directory:
                shutil.rmtree(d, ignore_errors=True)

    def query(
        self,
        key: str,
        *,
        max_posting: int | None = None,
        max_clips: int = 10,
        per_clip: int = 2,
        min_votes: int = 5,
        z: float = 6.0,
        chunk: int = 4_000_000,
    ) -> list[Candidate]:
        """Clips on other devices that share sound with clip ``key``, strongest first.

        A candidate needs at least ``min_votes`` votes at one offset (two adjacent 32 ms bins) and must stand ``z``
        standard deviations above the chance level for that pair of clips. Up to ``per_clip`` offsets are kept per
        other clip (repetitive music can agree at more than one), and up to ``max_clips`` other clips.
        """
        me = self.position[key]
        lm = np.load(self.clips[me].landmarks)
        if len(lm) == 0 or self.entries == 0:
            return []
        if max_posting is None:
            # Chance collisions average entries / 2^21 per hash; a hash seen far more often than that is noise.
            max_posting = max(50, int(20 * self.entries / (1 << 21)))
        q_hash = (lm >> np.uint64(32)).astype(np.uint32)
        q_t = (lm & _T_MASK).astype(np.int64)
        lo = np.searchsorted(self.hashes, q_hash, side="left")
        hi = np.searchsorted(self.hashes, q_hash, side="right")
        n = hi - lo
        keep = (n > 0) & (n <= max_posting)
        lo, n, q_t = lo[keep], n[keep], q_t[keep]
        if len(n) == 0:
            return []

        my_device = self._device[me]
        keys_parts: list[np.ndarray] = []
        ends = np.cumsum(n)
        begin = 0
        while begin < len(n):
            # Expand posting ranges chunk by chunk to bound memory.
            base = ends[begin - 1] if begin else 0
            end = int(np.searchsorted(ends, base + chunk, side="right"))
            end = max(end, begin + 1)
            cn, clo, ct = n[begin:end], lo[begin:end], q_t[begin:end]
            total = int(cn.sum())
            offsets = np.repeat(clo - (np.cumsum(cn) - cn), cn) + np.arange(total)
            post = np.asarray(self.postings[offsets])
            other = (post >> np.uint64(32)).astype(np.int64)
            dt = (post & _T_MASK).astype(np.int64) - np.repeat(ct, cn)
            ok = (other != me) & (self._device[other] != my_device)
            keys_parts.append((other[ok] << 32) | (dt[ok] + (1 << 31)))
            begin = end
        keys = np.concatenate(keys_parts) if keys_parts else np.zeros(0, dtype=np.int64)
        if len(keys) == 0:
            return []
        uniq, counts = np.unique(keys, return_counts=True)
        # Score an offset with its stronger neighbour: the two recordings' frames need not line up.
        pos_prev = np.searchsorted(uniq, uniq - 1)
        pos_next = np.searchsorted(uniq, uniq + 1)
        prev = np.where((pos_prev < len(uniq)) & (uniq[np.minimum(pos_prev, len(uniq) - 1)] == uniq - 1),
                        counts[np.minimum(pos_prev, len(uniq) - 1)], 0)  # fmt: skip
        nxt = np.where((pos_next < len(uniq)) & (uniq[np.minimum(pos_next, len(uniq) - 1)] == uniq + 1),
                       counts[np.minimum(pos_next, len(uniq) - 1)], 0)  # fmt: skip
        score = counts + np.maximum(prev, nxt)
        other = uniq >> 32
        dt = (uniq & 0xFFFFFFFF) - (1 << 31)
        # Weighted offset over the three bins.
        centre = (dt * counts + (dt - 1) * prev + (dt + 1) * nxt) / np.maximum(counts + prev + nxt, 1)

        votes_per_clip = np.bincount(other, weights=counts, minlength=len(self.clips))
        span = self._frames[me] + self._frames  # offsets at which two clips can overlap
        background = 2.0 * votes_per_clip / np.maximum(span, 1)
        bg = background[other]
        significant = (score >= min_votes) & (score >= bg + z * np.sqrt(bg) + 1)
        if not significant.any():
            return []
        idx = np.flatnonzero(significant)
        idx = idx[np.lexsort((-score[idx], other[idx]))]  # by clip, strongest first

        found: dict[int, list[int]] = {}
        for i in idx:
            c = int(other[i])
            picked = found.setdefault(c, [])
            if len(picked) >= per_clip or any(abs(dt[i] - dt[j]) <= 3 for j in picked):
                continue
            picked.append(int(i))
        best = sorted(found.items(), key=lambda kv: -score[kv[1][0]])[:max_clips]
        frame = frame_seconds(self.rate)
        return [
            Candidate(
                other=self.clips[c].key,
                offset_s=float(centre[i]) * frame,
                votes=int(score[i]),
                background=float(background[c]),
                total_votes=int(votes_per_clip[c]),
            )
            for c, picks in best
            for i in picks
        ]


# Per-process cache of opened indexes (worker processes answer many queries against one index).
_OPEN: dict[str, LandmarkIndex] = {}


def open_index(directory: str | Path) -> LandmarkIndex:
    key = str(directory)
    index = _OPEN.get(key)
    if index is None:
        _OPEN.clear()
        index = _OPEN[key] = LandmarkIndex(Path(directory))
    return index


def query_many(directory: str, keys: list[str], options: dict | None = None) -> list[tuple[str, list[Candidate]]]:
    """Worker-process entry point: query several clips against one index."""
    index = open_index(directory)
    return [(k, index.query(k, **(options or {}))) for k in keys]
