"""Media metadata from ffprobe (plus BWF chunks and camera sidecar files).

:func:`parse_probe` is a pure function of ffprobe's JSON so that metadata from
real cameras can be regression-tested from captured output alone.

Clip origin: a clip starts at its primary video stream's first frame (NLEs
place video clips by their first frame); audio-only files start at their audio.
Every audio stream's ``start_time`` relative to that origin becomes the
engine's ``audio_start_s``. That offset covers AAC priming handled through edit
lists, and MPEG-TS streams that start at 1.4 s with audio a few ms before video.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from mcsync.timecode import STANDARD_RATES, Timecode

from .riff import BwfMetadata, read_bwf
from .tools import FFmpegTools, find_tools, subprocess_flags

_VFR_TOLERANCE = 1e-3
_RATE_SNAP = 5e-4
_WAV_FORMATS = {"wav", "w64", "rf64"}


class ProbeError(RuntimeError):
    """ffprobe failed or the file has no usable streams."""


@dataclass(frozen=True)
class VideoStreamInfo:
    index: int
    codec: str
    width: int | None
    height: int | None
    frame_rate: Fraction
    avg_frame_rate: Fraction | None
    is_vfr: bool
    start_time_s: float
    duration_s: float | None
    rotation: int = 0
    frame_count: int | None = None


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int  # absolute stream index: ffmpeg -map 0:<index>
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str | None
    start_time_s: float
    duration_s: float | None
    bits_per_sample: int | None = None


@dataclass(frozen=True)
class TimecodeInfo:
    """Start timecode of a file.

    ``seconds`` is ``frames / rate``: the real time elapsed since the label
    00:00:00:00, which is what differences between clips must be computed
    from. Non-drop NTSC timecode (23.976, 29.97 NDF) runs 0.1 % slower than
    the wall clock while drop-frame and integer rates track it, so readings
    are only comparable within one ``family``.
    """

    seconds: float
    rate: Fraction | None
    drop_frame: bool
    source: str  # "tmcd" | "video" | "container" | "bwf"
    text: str | None = None

    @property
    def family(self) -> str:
        if self.rate is not None and self.rate.denominator == 1001 and not self.drop_frame:
            return "ntsc"
        return "wall"


@dataclass(frozen=True)
class MediaInfo:
    path: str
    size_bytes: int
    mtime_ns: int
    container: str
    duration_s: float
    video: tuple[VideoStreamInfo, ...]
    audio: tuple[AudioStreamInfo, ...]
    timecode: TimecodeInfo | None = None
    creation_time: datetime | None = None
    make: str | None = None
    model: str | None = None
    serial: str | None = None
    encoder: str | None = None
    bwf: BwfMetadata | None = None
    #: Container start time: FFmpeg's time zero when it decodes this file.
    format_start_s: float | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def primary_video(self) -> VideoStreamInfo | None:
        return self.video[0] if self.video else None

    @property
    def primary_audio(self) -> AudioStreamInfo | None:
        return self.audio[0] if self.audio else None

    @property
    def is_audio_only(self) -> bool:
        return not self.video

    @property
    def frame_rate(self) -> Fraction | None:
        return self.video[0].frame_rate if self.video else None

    @property
    def origin_s(self) -> float:
        """Container time of the clip's first frame (or first audio sample)."""
        if self.video:
            return self.video[0].start_time_s
        return self.audio[0].start_time_s if self.audio else 0.0

    def audio_start_s(self, stream: AudioStreamInfo | None = None) -> float:
        """Position of the extracted audio's first sample relative to the clip start.

        Extraction pads every stream to the container's time zero
        (``aresample=first_pts=0``), so the decoder's own timestamps position
        the audio. Only the container start matters, not the per-stream
        ``start_time``, which ffprobe versions report differently when edit
        lists delay a stream.
        """
        stream = stream or self.primary_audio
        if stream is None:
            return 0.0
        start = self.format_start_s if self.format_start_s is not None else stream.start_time_s
        return start - self.origin_s


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------


def probe(path: str | Path, tools: FFmpegTools | None = None, *, timeout_s: float = 120.0) -> MediaInfo:
    """Probe one media file (read-only)."""
    tools = tools or find_tools()
    path = Path(path)
    cmd = [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        flags = subprocess_flags()
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, **flags)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{path}: ffprobe timed out") from exc
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeError(f"{path}: {message[-1] if message else 'ffprobe failed'}")
    data = json.loads(proc.stdout or b"{}")
    stat = path.stat()
    fmt = str(data.get("format", {}).get("format_name", ""))
    bwf = read_bwf(path) if set(fmt.split(",")) & _WAV_FORMATS or path.suffix.lower() in (".wav", ".bwf") else None
    return parse_probe(
        data,
        path=str(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        bwf=bwf,
        sidecar=read_sidecar(path),
    )


def parse_probe(
    data: dict,
    *,
    path: str,
    size_bytes: int = 0,
    mtime_ns: int = 0,
    bwf: BwfMetadata | None = None,
    sidecar: dict[str, str] | None = None,
) -> MediaInfo:
    fmt = data.get("format", {})
    fmt_tags = _lower_keys(fmt.get("tags"))
    video: list[VideoStreamInfo] = []
    audio: list[AudioStreamInfo] = []
    tmcd: dict | None = None
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and not s.get("disposition", {}).get("attached_pic"):
            info = _video_stream(s)
            if info is not None:
                video.append(info)
        elif kind == "audio" and int(s.get("channels") or 0) > 0:
            audio.append(_audio_stream(s))
        elif kind == "data" and s.get("codec_tag_string") == "tmcd":
            tmcd = s
    if not video and not audio:
        raise ProbeError(f"{path}: no audio or video streams")

    duration = _float(fmt.get("duration"))
    primary = video[0] if video else audio[0]
    if primary.duration_s:
        duration = primary.duration_s
    if not duration or duration <= 0:
        raise ProbeError(f"{path}: unknown duration")

    video_tags = _lower_keys(_stream(data, video[0].index).get("tags")) if video else {}
    sidecar = sidecar or {}
    make, model, serial = _device_fields(fmt_tags, video_tags, data, bwf, sidecar)
    return MediaInfo(
        path=path,
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        container=str(fmt.get("format_name", "")),
        duration_s=float(duration),
        video=tuple(video),
        audio=tuple(audio),
        timecode=_timecode(tmcd, video, video_tags, fmt_tags, audio, bwf),
        creation_time=_creation_time(fmt_tags, video_tags),
        make=make,
        model=model,
        serial=serial,
        encoder=fmt_tags.get("encoder") or video_tags.get("encoder"),
        bwf=bwf,
        format_start_s=_float(fmt.get("start_time")),
        raw=data,
    )


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


def _lower_keys(tags: dict | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (tags or {}).items()}


def _stream(data: dict, index: int) -> dict:
    return next((s for s in data.get("streams", []) if s.get("index") == index), {})


def parse_rate(text: str | None) -> Fraction | None:
    """``"30000/1001"`` → Fraction, snapped to a standard rate when within 0.05 %."""
    if not text:
        return None
    num, _, den = str(text).partition("/")
    try:
        rate = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        try:
            rate = Fraction(float(text)).limit_denominator(1001)
        except (ValueError, OverflowError):
            return None
    if rate <= 0:
        return None
    for std in STANDARD_RATES.values():
        if abs(float(rate - std)) <= _RATE_SNAP * float(std):
            return std
    return rate


def _video_stream(s: dict) -> VideoStreamInfo | None:
    r = parse_rate(s.get("r_frame_rate"))
    avg = parse_rate(s.get("avg_frame_rate"))
    rate = r or avg
    if rate is None:
        return None
    # Some VFR sources report a timebase-like r_frame_rate (e.g. 600/1).
    if avg is not None and r is not None and r > 2 * avg:
        rate = avg
    is_vfr = avg is not None and r is not None and abs(float(avg - r)) > _VFR_TOLERANCE * float(r)
    rotation = 0
    for side in s.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(float(side["rotation"]))
    if not rotation and "rotate" in (s.get("tags") or {}):
        rotation = int(float(s["tags"]["rotate"]))
    return VideoStreamInfo(
        index=int(s["index"]),
        codec=str(s.get("codec_name", "unknown")),
        width=s.get("width"),
        height=s.get("height"),
        frame_rate=rate,
        avg_frame_rate=avg,
        is_vfr=is_vfr,
        start_time_s=_float(s.get("start_time")) or 0.0,
        duration_s=_float(s.get("duration")),
        rotation=rotation % 360,
        frame_count=int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None,
    )


def _audio_stream(s: dict) -> AudioStreamInfo:
    bits = s.get("bits_per_raw_sample") or s.get("bits_per_sample")
    return AudioStreamInfo(
        index=int(s["index"]),
        codec=str(s.get("codec_name", "unknown")),
        sample_rate=int(s.get("sample_rate") or 0),
        channels=int(s.get("channels") or 0),
        channel_layout=s.get("channel_layout"),
        start_time_s=_float(s.get("start_time")) or 0.0,
        duration_s=_float(s.get("duration")),
        bits_per_sample=int(bits) if str(bits or "").isdigit() and int(bits) > 0 else None,
    )


# ---------------------------------------------------------------------------
# Timecode, creation time, device
# ---------------------------------------------------------------------------


def _parse_tc(text: str | None, rate: Fraction | None, source: str) -> TimecodeInfo | None:
    if not text or rate is None:
        return None
    try:
        tc = Timecode.parse(text)
        seconds = float(tc.to_seconds(rate))
    except ValueError:
        return None
    return TimecodeInfo(seconds=seconds, rate=rate, drop_frame=tc.drop_frame, source=source, text=str(tc))


def _timecode(tmcd, video, video_tags, fmt_tags, audio, bwf) -> TimecodeInfo | None:
    video_rate = video[0].frame_rate if video else None
    if tmcd is not None:
        rate = parse_rate(tmcd.get("avg_frame_rate")) or parse_rate(tmcd.get("r_frame_rate")) or video_rate
        tc = _parse_tc((tmcd.get("tags") or {}).get("timecode"), rate, "tmcd")
        if tc:
            return tc
    for tags, source in ((video_tags, "video"), (fmt_tags, "container")):
        tc = _parse_tc(tags.get("timecode"), video_rate, source)
        if tc:
            return tc
    reference = bwf.time_reference if bwf is not None else None
    if reference is None and fmt_tags.get("time_reference", "").isdigit():
        reference = int(fmt_tags["time_reference"])
    if reference is not None and audio and audio[0].sample_rate > 0:
        rate = bwf.timecode_rate if bwf is not None else None
        drop = bool(bwf.timecode_drop_frame) if bwf is not None else False
        seconds = reference / audio[0].sample_rate
        text = None
        if rate is not None:
            try:
                text = str(Timecode.from_frames(int(seconds * rate), rate, drop and rate.denominator == 1001))
            except ValueError:
                text = None
        return TimecodeInfo(seconds=seconds, rate=rate, drop_frame=drop, source="bwf", text=text)
    return None


def _parse_datetime(text: str | None) -> datetime | None:
    if not text:
        return None
    t = text.strip().replace(" ", "T", 1)
    if re.match(r".*[+-]\d{4}$", t):  # +0200 → +02:00
        t = t[:-2] + ":" + t[-2:]
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.year < 1990:  # unset camera clocks write 1904/1970 epochs
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _creation_time(fmt_tags: dict[str, str], video_tags: dict[str, str]) -> datetime | None:
    # Apple's key carries the local offset, so it is an exact instant.
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        for tags in (fmt_tags, video_tags):
            dt = _parse_datetime(tags.get(key))
            if dt is not None:
                return dt
    return None


def _device_fields(fmt_tags, video_tags, data, bwf, sidecar) -> tuple[str | None, str | None, str | None]:
    make = sidecar.get("make") or fmt_tags.get("com.apple.quicktime.make") or fmt_tags.get("com.android.manufacturer")
    model = sidecar.get("model") or fmt_tags.get("com.apple.quicktime.model") or fmt_tags.get("com.android.model")
    make = make or fmt_tags.get("make") or fmt_tags.get("manufacturer")
    model = model or fmt_tags.get("model")
    serial = sidecar.get("serial") or fmt_tags.get("serial_number") or fmt_tags.get("com.apple.quicktime.serial")
    handler = video_tags.get("handler_name", "")
    encoder = fmt_tags.get("encoder", "") + " " + video_tags.get("encoder", "")
    if not make and ("GoPro" in handler or "GoPro" in encoder):
        make = "GoPro"
    if not make and ("DJI" in handler or "DJI" in encoder):
        make, model = "DJI", model or encoder.strip() or None
    if not make and fmt_tags.get("company_name") and fmt_tags.get("company_name") != "FFmpeg":  # MXF identification
        make, model = fmt_tags.get("company_name"), fmt_tags.get("product_name")
    if not make and bwf is not None and bwf.originator:
        make, model = bwf.originator, bwf.originator_reference
    return make or None, model or None, serial or None


def read_sidecar(path: Path) -> dict[str, str]:
    """Device fields from a Sony/Canon-style XML sidecar (``C0001M01.XML``)."""
    for candidate in (path.with_name(path.stem + "M01.XML"), path.with_name(path.stem + "M01.xml")):
        if candidate.is_file() and candidate.stat().st_size < 1 << 20:
            try:
                root = ET.parse(candidate).getroot()
            except (ET.ParseError, OSError):
                return {}
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "Device":
                    out = {
                        "make": node.get("manufacturer"),
                        "model": node.get("modelName"),
                        "serial": node.get("serialNo"),
                    }
                    return {k: v for k, v in out.items() if v}
            return {}
    return {}


def epoch_seconds(dt: datetime) -> float:
    return dt.timestamp()


def is_media_candidate(path: Path) -> bool:
    """Cheap filter before probing: skip sidecars, thumbnails and hidden files."""
    name = path.name
    if name.startswith(".") or not path.is_file():
        return False
    return path.suffix.lower() not in {
        ".xml", ".thm", ".lrv", ".jpg", ".jpeg", ".png", ".txt", ".ini", ".db", ".bin", ".xmp", ".srt",
        ".pdf", ".log", ".cpi", ".bdm", ".mpl", ".ppn", ".dat", ".json", ".mcsync",
    }  # fmt: skip


__all__ = [
    "AudioStreamInfo",
    "MediaInfo",
    "ProbeError",
    "TimecodeInfo",
    "VideoStreamInfo",
    "epoch_seconds",
    "is_media_candidate",
    "parse_probe",
    "parse_rate",
    "probe",
    "read_sidecar",
]
