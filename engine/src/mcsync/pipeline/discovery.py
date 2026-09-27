"""Finding media files on disk, incrementally.

Folders are walked with ``os.scandir`` (one system call per directory, file sizes and times included), without
following links to directories, and results come back in small batches so a card dump of thousands of files starts
being processed, and counted in the interface, while the rest is still being listed.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

from mcsync.media.probe import is_media_name, media_kind_guess

#: Folders that never hold footage: operating-system bookkeeping and trash.
SKIP_DIRS = {
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".DocumentRevisions-V100", "$RECYCLE.BIN",
    "System Volume Information", "__MACOSX", ".git",
}  # fmt: skip
BATCH_FILES = 256
BATCH_SECONDS = 0.25


@dataclass(frozen=True)
class Found:
    path: str
    size: int
    mtime_ns: int
    kind: str | None  # from the extension: 'video' | 'audio' | None


@dataclass
class WalkProblem:
    path: str
    message: str


def _entry(path: str, st: os.stat_result) -> Found:
    return Found(path, st.st_size, st.st_mtime_ns, media_kind_guess(path))


def walk(
    roots: Iterable[str],
    *,
    recursive: bool = True,
    problems: list[WalkProblem] | None = None,
    should_stop=lambda: False,
) -> Iterator[list[Found]]:
    """Media files under ``roots`` (files or folders), in batches of up to ``BATCH_FILES`` or every
    ``BATCH_SECONDS``. Unreadable folders are reported in ``problems`` and skipped."""
    batch: list[Found] = []
    last = time.monotonic()
    stack: list[str] = []
    for root in roots:
        try:
            st = os.stat(root)
        except OSError as exc:
            if problems is not None:
                problems.append(WalkProblem(root, exc.strerror or str(exc)))
            continue
        if os.path.isdir(root):
            stack.append(root)
        elif is_media_name(os.path.basename(root)):
            batch.append(_entry(root, st))
    while stack:
        if should_stop():
            return
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            if problems is not None:
                problems.append(WalkProblem(folder, exc.strerror or str(exc)))
            continue
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if recursive and e.name not in SKIP_DIRS and not e.name.startswith("."):
                        subdirs.append(e.path)
                elif e.is_file() and is_media_name(e.name):
                    batch.append(_entry(e.path, e.stat()))
            except OSError:
                continue
            if len(batch) >= BATCH_FILES or (batch and time.monotonic() - last >= BATCH_SECONDS):
                yield batch
                batch, last = [], time.monotonic()
        stack.extend(reversed(subdirs))  # depth first, in name order
    if batch:
        yield batch


_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


def volume_of(path: str) -> str:
    """The drive or share a path lives on, as the user knows it: ``E:``, ``\\\\server\\share``,
    ``/Volumes/SHOOT_01``, ``/media/me/CARD``; else the top-level folder."""
    if _WIN_DRIVE.match(path) or path.startswith("\\\\") or ("\\" in path and sys.platform == "win32"):
        p: PurePath = PureWindowsPath(path)
        return p.drive or p.anchor
    p = PurePosixPath(path)
    parts = p.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return str(PurePosixPath(*parts[:3]))
    for prefix in (("media",), ("mnt",), ("run", "media")):
        n = len(prefix)
        if tuple(parts[1 : 1 + n]) == prefix and len(parts) > 1 + n:
            depth = 1 + n + (1 if prefix != ("mnt",) and len(parts) > 2 + n else 0)
            return str(PurePosixPath(*parts[: depth + 1]))
    return str(PurePosixPath(*parts[:2])) if len(parts) > 1 else str(p)


def volume_online(volume: str) -> bool:
    return os.path.exists(volume if not _WIN_DRIVE.fullmatch(volume) else volume + "\\")
