"""Poster frames for the media browser, cached by content fingerprint.

One frame (10 % into the clip, at most 5 s) is decoded with FFmpeg as raw RGB at 192 × 108 (letterboxed) and stored
as a PNG written here with zlib: FFmpeg ships without image encoders. A file's thumbnail is made once; moving or
renaming the file keeps it (the key is the content fingerprint).
"""

from __future__ import annotations

import os
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

from .probe import MediaInfo
from .tools import FFmpegTools, subprocess_flags

WIDTH, HEIGHT = 192, 108
VERSION = 1


def thumbnail_path(cache_root: Path, fingerprint: str) -> Path:
    return cache_root / "thumbs" / fingerprint[:2] / f"{fingerprint}-v{VERSION}.png"


def encode_png(rgb: bytes, width: int, height: int) -> bytes:
    """A minimal RGB8 PNG (filter 0 on every row)."""
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def make_thumbnail(info: MediaInfo, fingerprint: str, cache_root: Path, tools: FFmpegTools) -> Path | None:
    """The clip's poster frame (made now if needed); None for audio-only or undecodable files."""
    if not info.video:
        return None
    out = thumbnail_path(cache_root, fingerprint)
    if out.is_file():
        return out
    at = min(5.0, max(0.0, info.duration_s * 0.1))
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0x383535"
    )
    cmd = [
        tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-ss", f"{at:.3f}", "-i", info.path,
        "-map", "0:v:0", "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, **subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or len(proc.stdout) < WIDTH * HEIGHT * 3:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(encode_png(proc.stdout[: WIDTH * HEIGHT * 3], WIDTH, HEIGHT))
    os.replace(tmp, out)
    return out
