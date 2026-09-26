"""Locating the FFmpeg binaries.

Search order:

1. ``MCSYNC_FFMPEG_DIR``: a directory containing ``ffmpeg`` and ``ffprobe``
   (the desktop app sets this to its bundled LGPL build);
2. an ``ffmpeg`` directory next to a frozen (PyInstaller) engine executable;
3. the system ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ENV_DIR = "MCSYNC_FFMPEG_DIR"


class FFmpegNotFound(RuntimeError):
    """ffmpeg/ffprobe could not be located."""


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str

    def version(self) -> str:
        out = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            **subprocess_flags(),
        )
        return out.stdout.splitlines()[0] if out.stdout else ""


def subprocess_flags() -> dict:
    """Keep FFmpeg from flashing a console window when the GUI app runs it on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _from_dir(directory: Path) -> FFmpegTools | None:
    ffmpeg, ffprobe = directory / _exe("ffmpeg"), directory / _exe("ffprobe")
    if ffmpeg.is_file() and ffprobe.is_file():
        return FFmpegTools(str(ffmpeg), str(ffprobe))
    return None


@lru_cache(maxsize=1)
def find_tools() -> FFmpegTools:
    candidates: list[Path] = []
    if os.environ.get(ENV_DIR):
        candidates.append(Path(os.environ[ENV_DIR]))
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [base / "ffmpeg", Path(sys.executable).parent / "ffmpeg"]
    for directory in candidates:
        tools = _from_dir(directory)
        if tools is not None:
            return tools
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return FFmpegTools(ffmpeg, ffprobe)
    raise FFmpegNotFound(f"ffmpeg and ffprobe were not found (set {ENV_DIR}, or install FFmpeg and add it to PATH)")
