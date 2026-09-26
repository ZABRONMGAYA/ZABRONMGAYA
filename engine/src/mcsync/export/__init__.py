"""Timeline export to NLE XML: FCP 7 XML (xmeml) for Premiere Pro and Resolve, FCPXML for Resolve.

Exports reference the original media files; nothing is copied or re-encoded.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from mcsync.project.db import ClipRow
from mcsync.timeline import Timeline

from .fcpxml import write_fcpxml
from .sequence import ExportClip, ExportError, ExportOptions, ExportSequence, SourceMedia, Track, build_sequence
from .urls import file_url
from .xmeml import write_xmeml

FORMATS = {"xmeml": write_xmeml, "fcpxml": write_fcpxml}
EXTENSIONS = {"xmeml": ".xml", "fcpxml": ".fcpxml"}

__all__ = [
    "EXTENSIONS",
    "FORMATS",
    "ExportClip",
    "ExportError",
    "ExportOptions",
    "ExportSequence",
    "SourceMedia",
    "Track",
    "build_sequence",
    "export_timeline",
    "file_url",
    "format_report",
    "write_fcpxml",
    "write_xmeml",
]


def format_report(seq: ExportSequence, fmt: str) -> dict:
    """The export report, with placement errors as the format's readers will see them."""
    if fmt == "fcpxml":
        return seq.report(subframe_in=True)
    # xmeml: Resolve rounds in points to whole frames; Premiere Pro reads the sub-frame ticks.
    report = seq.report(subframe_in=False)
    report["max_error_ms_premiere"] = seq.report(subframe_in=True)["max_error_ms"]
    return report


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        # mkstemp makes the file private; an export is an ordinary document (and editors share workstations).
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export_timeline(
    timeline: Timeline,
    rows: Mapping[int, ClipRow],
    fmt: str,
    path: str | Path,
    options: ExportOptions | None = None,
) -> dict:
    """Write ``timeline`` as ``fmt`` ("xmeml" or "fcpxml") to ``path``; return the report."""
    if fmt not in FORMATS:
        raise ExportError(f"unknown export format {fmt!r}; choose one of {', '.join(FORMATS)}")
    seq = build_sequence(timeline, rows, options)
    _write_atomic(Path(path), FORMATS[fmt](seq))
    return {"path": str(path), "format": fmt} | format_report(seq, fmt)
