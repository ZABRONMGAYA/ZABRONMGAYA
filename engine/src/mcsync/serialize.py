"""JSON conversion for engine data (project database and JSON-RPC).

Rules: dataclasses become objects, enums their values, tuples lists,
``Fraction`` a ``"num/den"`` string, datetimes ISO 8601, and non-finite floats
``null`` (``Infinity`` is not valid JSON and JavaScript rejects it). The
``*_from_dict`` functions restore the types the engine works with.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from mcsync.media.probe import AudioStreamInfo, MediaInfo, TimecodeInfo, VideoStreamInfo
from mcsync.media.riff import BwfMetadata
from mcsync.sync.types import (
    Candidate,
    ClipPlacement,
    Flag,
    MatchStatus,
    OffsetEstimate,
    PairwiseMatch,
    PlacementMethod,
    PlacementStatus,
    WindowMeasurement,
)


def to_jsonable(obj: Any, *, skip: frozenset[str] = frozenset()) -> Any:
    """Recursively convert to JSON-compatible values. ``skip`` drops dataclass fields by name."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.generic):
        return to_jsonable(obj.item())
    if isinstance(obj, Fraction):
        return f"{obj.numerator}/{obj.denominator}"
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: to_jsonable(getattr(obj, f.name), skip=skip)
            for f in dataclasses.fields(obj)
            if f.name not in skip and not f.name.startswith("_")
        }
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, skip=skip) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v, skip=skip) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    raise TypeError(f"cannot convert {type(obj).__name__} to JSON")


def _inf(v: float | None) -> float:
    return float("inf") if v is None else float(v)


def _fraction(v: str | None) -> Fraction | None:
    if v is None:
        return None
    num, _, den = v.partition("/")
    return Fraction(int(num), int(den or 1))


# ---------------------------------------------------------------------------
# Sync results
# ---------------------------------------------------------------------------


def estimate_from_dict(d: dict) -> OffsetEstimate:
    return OffsetEstimate(
        offset_s=d["offset_s"],
        confidence=d["confidence"],
        status=MatchStatus(d["status"]),
        offset_time_s=d.get("offset_time_s", 0.0),
        drift_ppm=d.get("drift_ppm", 0.0),
        drift_std_ppm=_inf(d.get("drift_std_ppm")),
        std_error_s=_inf(d.get("std_error_s")),
        overlap_s=d.get("overlap_s", 0.0),
        coarse_psr=d.get("coarse_psr", 0.0),
        uniqueness=_inf(d.get("uniqueness")),
        n_windows=d.get("n_windows", 0),
        inlier_fraction=d.get("inlier_fraction", 0.0),
        correlation=d.get("correlation", 0.0),
        flags=tuple(Flag(f) for f in d.get("flags", ())),
        windows=tuple(WindowMeasurement(**w) for w in d.get("windows", ())),
        alternatives=tuple(Candidate(**a) for a in d.get("alternatives", ())),
    )


def match_from_dict(d: dict) -> PairwiseMatch:
    window = d.get("search_window")
    return PairwiseMatch(
        ref_id=d["ref_id"],
        tgt_id=d["tgt_id"],
        offset_s=d["offset_s"],
        estimate=estimate_from_dict(d["estimate"]),
        offset_time_s=d.get("offset_time_s", 0.0),
        audio_shift_s=d.get("audio_shift_s", 0.0),
        search_window=tuple(window) if window is not None else None,  # type: ignore[arg-type]
        flags=tuple(Flag(f) for f in d.get("flags", ())),
    )


def placement_from_dict(d: dict) -> ClipPlacement:
    return ClipPlacement(
        clip_id=d["clip_id"],
        start_s=d["start_s"],
        group=d["group"],
        method=PlacementMethod(d["method"]),
        confidence=d["confidence"],
        status=PlacementStatus(d["status"]),
        flags=tuple(Flag(f) for f in d.get("flags", ())),
        drift_ppm=d.get("drift_ppm", 0.0),
    )


# ---------------------------------------------------------------------------
# Media metadata
# ---------------------------------------------------------------------------


def media_info_to_dict(info: MediaInfo, *, include_raw: bool = True) -> dict:
    return to_jsonable(info, skip=frozenset() if include_raw else frozenset({"raw"}))


def media_info_from_dict(d: dict) -> MediaInfo:
    tc = d.get("timecode")
    bwf = d.get("bwf")
    return MediaInfo(
        path=d["path"],
        size_bytes=d["size_bytes"],
        mtime_ns=d["mtime_ns"],
        container=d["container"],
        duration_s=d["duration_s"],
        video=tuple(
            VideoStreamInfo(
                **{**v, "frame_rate": _fraction(v["frame_rate"]), "avg_frame_rate": _fraction(v.get("avg_frame_rate"))}
            )
            for v in d.get("video", ())
        ),
        audio=tuple(AudioStreamInfo(**a) for a in d.get("audio", ())),
        timecode=TimecodeInfo(**{**tc, "rate": _fraction(tc.get("rate"))}) if tc else None,
        creation_time=datetime.fromisoformat(d["creation_time"]) if d.get("creation_time") else None,
        make=d.get("make"),
        model=d.get("model"),
        serial=d.get("serial"),
        encoder=d.get("encoder"),
        bwf=BwfMetadata(
            **{
                **bwf,
                "timecode_rate": _fraction(bwf.get("timecode_rate")),
                "track_names": tuple(bwf.get("track_names", ())),
            }
        )
        if bwf
        else None,
        format_start_s=d.get("format_start_s"),
        raw=d.get("raw") or {},
    )
