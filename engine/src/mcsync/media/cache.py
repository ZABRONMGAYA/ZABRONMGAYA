"""On-disk analysis cache.

Layout (``v1`` is the cache format version)::

    <root>/v1/<fp[:2]>/<fingerprint>/a<stream>[c<channel>]-<params>/
        pcm.f32        prepared analysis signal: float32 LE, mono, analysis rate
        meta.json      rate, samples, level, source; written last (= entry complete)
        peaks_<n>.i8   waveform overview levels
        last_used      touched on every load (LRU eviction)

Entries are created in a temporary sibling directory and renamed into place,
so a crash or cancel never leaves a half-written entry that looks complete.
The prepared signal is memory-mapped on load: a 3-hour recording costs 345 MB
of disk and only the pages the matcher touches.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from mcsync.sync.params import DEFAULT_PARAMS, SyncParams
from mcsync.sync.signal import AnalysisSignal

CACHE_VERSION = "v1"
#: Bumped whenever extraction output changes (2: padded to the container start).
EXTRACTION_VERSION = 2
DEFAULT_MAX_BYTES = 20 * 2**30


def default_cache_dir() -> Path:
    if env := os.environ.get("MCSYNC_CACHE_DIR"):
        return Path(env)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "MulticamSync"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "MulticamSync" / "Cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "multicam-sync"


def params_key(params: SyncParams) -> str:
    """Hash of the parameters that shape the prepared signal."""
    text = f"{params.analysis_rate}:{params.band_low_hz}:{params.band_high_hz}:bp4:{EXTRACTION_VERSION}"
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:10]


@dataclass(frozen=True)
class CacheEntry:
    directory: Path

    @property
    def pcm_path(self) -> Path:
        return self.directory / "pcm.f32"

    @property
    def meta_path(self) -> Path:
        return self.directory / "meta.json"

    def exists(self) -> bool:
        return self.meta_path.is_file()

    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text())

    def touch(self) -> None:
        (self.directory / "last_used").write_text(str(time.time()))

    def load(self) -> AnalysisSignal:
        """Memory-map the prepared signal."""
        meta = self.meta()
        self.touch()
        level = float(meta["level_dbfs"]) if meta["level_dbfs"] is not None else float("-inf")
        if meta["samples"] == 0:
            samples = np.zeros(0, dtype=np.float32)
        else:
            samples = np.memmap(self.pcm_path, dtype="<f4", mode="r", shape=(int(meta["samples"]),))
        return AnalysisSignal(samples=samples, rate=int(meta["rate"]), level_dbfs=level)

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())


class AnalysisCache:
    def __init__(self, root: str | Path | None = None, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()
        self.max_bytes = max_bytes

    @property
    def base(self) -> Path:
        return self.root / CACHE_VERSION

    def entry(
        self, fingerprint: str, stream_index: int, *, channel: int | None = None, params: SyncParams = DEFAULT_PARAMS
    ) -> CacheEntry:
        name = f"a{stream_index}" + (f"c{channel}" if channel is not None else "") + f"-{params_key(params)}"
        return CacheEntry(self.base / fingerprint[:2] / fingerprint / name)

    def entries(self) -> list[CacheEntry]:
        if not self.base.is_dir():
            return []
        return [CacheEntry(d) for d in self.base.glob("*/*/*") if d.is_dir() and (d / "meta.json").is_file()]

    def size_bytes(self) -> int:
        return sum(e.size_bytes() for e in self.entries())

    def evict(self, keep: set[Path] | frozenset[Path] = frozenset()) -> int:
        """Delete least-recently used entries until the cache fits ``max_bytes``.

        Entries in ``keep`` (open projects) are never deleted. Returns bytes freed.
        """
        entries = [(e, e.size_bytes()) for e in self.entries()]
        total = sum(size for _, size in entries)

        def last_used(e: CacheEntry) -> float:
            marker = e.directory / "last_used"
            return marker.stat().st_mtime if marker.exists() else e.meta_path.stat().st_mtime

        freed = 0
        for entry, size in sorted(entries, key=lambda es: last_used(es[0])):
            if total - freed <= self.max_bytes:
                break
            if entry.directory in keep:
                continue
            shutil.rmtree(entry.directory, ignore_errors=True)
            freed += size
        return freed

    def clear(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)
