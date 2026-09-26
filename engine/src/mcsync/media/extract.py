"""Streaming extraction of the analysis signal with FFmpeg.

FFmpeg decodes one audio stream, downmixes it (or picks one channel) and
resamples it to the analysis rate. The engine reads the raw float32 output in
1 MiB chunks and, chunk by chunk:

* feeds the waveform-overview builder;
* applies the analysis band-pass, carrying the filter state across chunks, so
  the result equals :func:`mcsync.sync.prepare_signal` on the whole signal;
* writes the filtered samples to a temporary cache entry while accumulating
  their energy.

A final pass normalises the file in place to unit RMS. Memory use is a few MB
whatever the recording length. ``aresample=async=1`` fills timestamp gaps with
silence (damaged or dropout-affected streams) and ``first_pts=0`` pads a
delayed stream from the container's time zero, so sample *n* is always *n /
rate* seconds after the container starts, whatever the stream metadata says.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np
from scipy import signal as sps

from mcsync.sync.engine import CancelToken
from mcsync.sync.params import DEFAULT_PARAMS, SyncParams
from mcsync.sync.signal import AnalysisSignal, band_filter

from .cache import CacheEntry
from .probe import AudioStreamInfo, MediaInfo
from .tools import FFmpegTools, find_tools, subprocess_flags
from .waveform import PeakBuilder, write_peaks

_CHUNK_BYTES = 1 << 20
_NORMALISE_BLOCK = 1 << 20


class ExtractionError(RuntimeError):
    """FFmpeg could not decode the stream."""


class ExtractionCancelled(RuntimeError):
    """The cancel token was set during extraction."""


def ffmpeg_command(
    tools: FFmpegTools, path: str, stream_index: int, rate: int, channel: int | None = None, channels: int = 1
) -> list[str]:
    """Decode one stream to mono float32 at ``rate``.

    Channels are averaged explicitly (FFmpeg's own downmix scales stereo by
    1/√2), so levels match :func:`mcsync.sync.prepare_signal`.
    """
    if channel is not None:
        mix = [f"pan=mono|c0=c{channel}"]
    elif channels > 1:
        mix = ["pan=mono|c0=" + "+".join(f"{1 / channels:.10g}*c{k}" for k in range(channels))]
    else:
        mix = []
    # first_pts=0 pads the stream to the container's time zero (see MediaInfo.audio_start_s).
    filters = mix + ["aresample=async=1:first_pts=0"]
    cmd = [
        tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
        "-i", path,
        "-map", f"0:{stream_index}", "-vn", "-sn", "-dn",
        "-af", ",".join(filters), "-ar", str(rate),
    ]  # fmt: skip
    return cmd + ["-ac", "1", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]


def extract_to_cache(
    info: MediaInfo,
    entry: CacheEntry,
    *,
    stream: AudioStreamInfo | None = None,
    channel: int | None = None,
    params: SyncParams = DEFAULT_PARAMS,
    tools: FFmpegTools | None = None,
    cancel: CancelToken | None = None,
    progress: Callable[[float], None] | None = None,
) -> AnalysisSignal:
    """Return the cached analysis signal for one audio stream, extracting it if needed."""
    if entry.exists():
        return entry.load()
    stream = stream or info.primary_audio
    if stream is None:
        raise ExtractionError(f"{info.path}: no audio stream")
    if channel is not None and not 0 <= channel < stream.channels:
        raise ValueError(f"{info.path}: stream {stream.index} has no channel {channel}")
    tools = tools or find_tools()

    entry.directory.parent.mkdir(parents=True, exist_ok=True)
    tmp = entry.directory.parent / f".{entry.directory.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.mkdir()
    try:
        cmd = ffmpeg_command(tools, info.path, stream.index, params.analysis_rate, channel, stream.channels)
        expected = (stream.duration_s or info.duration_s) * params.analysis_rate
        n, sumsq, peaks = _decode_filtered(cmd, tmp / "pcm.f32", params, expected, cancel, progress)
        level = _normalise(tmp / "pcm.f32", n, sumsq)
        write_peaks(tmp, peaks)
        meta = {
            "version": 1,
            "rate": params.analysis_rate,
            "samples": n,
            "level_dbfs": level if math.isfinite(level) else None,
            "source": {"path": info.path, "stream": stream.index, "channel": channel},
            "created": time.time(),
        }
        (tmp / "meta.json").write_text(json.dumps(meta))
        try:
            tmp.rename(entry.directory)
        except OSError:
            if not entry.exists():  # someone else finished first: theirs is as good as ours
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return entry.load()


def _decode_filtered(
    cmd: list[str],
    out_path: Path,
    params: SyncParams,
    expected_samples: float,
    cancel: CancelToken | None,
    progress: Callable[[float], None] | None,
) -> tuple[int, float, dict[int, np.ndarray]]:
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **subprocess_flags()
    )
    assert proc.stdout is not None and proc.stderr is not None
    errors: deque[str] = deque(maxlen=20)
    drain = threading.Thread(
        target=lambda: errors.extend(line.decode(errors="replace").rstrip() for line in proc.stderr),  # type: ignore[union-attr]
        daemon=True,
    )
    drain.start()

    sos = band_filter(params)
    zi = np.zeros((sos.shape[0], 2))
    builder = PeakBuilder()
    rest = b""
    n, sumsq = 0, 0.0
    try:
        with open(out_path, "wb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise ExtractionCancelled()
                buf = proc.stdout.read(_CHUNK_BYTES)
                if not buf:
                    break
                buf = rest + buf
                usable = len(buf) // 4 * 4
                rest = buf[usable:]
                x = np.frombuffer(buf[:usable], dtype="<f4")
                builder.feed(x)
                y, zi = sps.sosfilt(sos, x, zi=zi)
                sumsq += float(np.dot(y, y))
                out.write(y.astype("<f4").tobytes())
                n += len(x)
                if progress is not None and expected_samples > 0:
                    progress(min(n / expected_samples, 1.0))
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        drain.join(timeout=5)
    if proc.returncode != 0:
        raise ExtractionError(errors[-1] if errors else f"ffmpeg exited with status {proc.returncode}")
    return n, sumsq, builder.finish()


def _normalise(path: Path, n: int, sumsq: float) -> float:
    """Scale the file to unit RMS in place; return the in-band level in dBFS."""
    rms = math.sqrt(sumsq / n) if n else 0.0
    if n == 0:
        return float("-inf")
    data = np.memmap(path, dtype="<f4", mode="r+", shape=(n,))
    if rms > 1e-12:
        scale = np.float32(1.0 / rms)
        for i in range(0, n, _NORMALISE_BLOCK):
            data[i : i + _NORMALISE_BLOCK] *= scale
        level = 20.0 * math.log10(rms)
    else:
        data[:] = 0.0
        level = float("-inf")
    data.flush()
    del data
    return level
