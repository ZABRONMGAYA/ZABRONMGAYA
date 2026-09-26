"""Broadcast WAV metadata read straight from the RIFF chunks.

ffprobe exposes the ``bext`` time reference but not the iXML chunk, which is
where recorders (Sound Devices, Zoom, Tascam, Aaton, ...) store the timecode
rate and drop-frame flag needed to interpret it. Supports RIFF and RF64/BW64
(recordings over 4 GB, i.e. long recorder takes).
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

_MAX_META_CHUNK = 1 << 20  # bext and iXML are a few kB; refuse anything absurd


@dataclass(frozen=True)
class BwfMetadata:
    time_reference: int | None = None  # samples since midnight
    originator: str | None = None
    originator_reference: str | None = None
    description: str | None = None
    timecode_rate: Fraction | None = None
    timecode_drop_frame: bool | None = None
    project: str | None = None
    scene: str | None = None
    take: str | None = None
    tape: str | None = None
    track_names: tuple[str, ...] = ()


def _text(raw: bytes) -> str | None:
    s = raw.split(b"\0", 1)[0].decode("latin-1").strip()
    return s or None


def _parse_rate(text: str | None) -> Fraction | None:
    if not text:
        return None
    try:
        num, _, den = text.strip().partition("/")
        rate = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def read_bwf(path: str | Path) -> BwfMetadata | None:
    """bext + iXML metadata of a WAV/RF64 file, or None if it is not one."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[8:12] != b"WAVE" or header[:4] not in (b"RIFF", b"RF64", b"BW64"):
                return None
            big_sizes: dict[bytes, int] = {}
            chunks: dict[bytes, bytes] = {}
            while True:
                head = f.read(8)
                if len(head) < 8:
                    break
                cid, size = head[:4], struct.unpack("<I", head[4:])[0]
                if size == 0xFFFFFFFF:
                    size = big_sizes.get(cid, 0)
                if cid in (b"bext", b"iXML", b"ds64") and size <= _MAX_META_CHUNK:
                    body = f.read(size)
                    chunks[cid] = body
                    if cid == b"ds64" and len(body) >= 24:
                        # riff size, data size, sample count (all 64-bit)
                        big_sizes[b"data"] = struct.unpack("<Q", body[8:16])[0]
                else:
                    f.seek(size, 1)
                if size % 2:
                    f.seek(1, 1)
    except OSError:
        return None

    fields: dict = {}
    bext = chunks.get(b"bext")
    if bext and len(bext) >= 346:
        fields["description"] = _text(bext[0:256])
        fields["originator"] = _text(bext[256:288])
        fields["originator_reference"] = _text(bext[288:320])
        low, high = struct.unpack("<II", bext[338:346])
        fields["time_reference"] = (high << 32) | low
    ixml = chunks.get(b"iXML")
    if ixml:
        extra = _parse_ixml(ixml)
        if fields.get("time_reference") is not None:
            extra.pop("time_reference", None)  # bext is authoritative
        fields.update(extra)
    return BwfMetadata(**fields)


def _parse_ixml(raw: bytes) -> dict:
    try:
        root = ET.fromstring(raw.split(b"\0", 1)[0])
    except ET.ParseError:
        return {}

    def find(tag: str) -> str | None:
        node = root.find(f".//{tag}")
        return node.text.strip() if node is not None and node.text else None

    out: dict = {
        "project": find("PROJECT"),
        "scene": find("SCENE"),
        "take": find("TAKE"),
        "tape": find("TAPE"),
        "timecode_rate": _parse_rate(find("TIMECODE_RATE")),
    }
    flag = find("TIMECODE_FLAG")
    if flag:
        out["timecode_drop_frame"] = flag.upper() == "DF"
    if out.get("timecode_rate") is None:
        out.pop("timecode_rate")
    hi, lo = find("TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI"), find("TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO")
    if hi is not None and lo is not None and hi.isdigit() and lo.isdigit():
        out["time_reference"] = (int(hi) << 32) | int(lo)
    names = [n.text.strip() for n in root.findall(".//TRACK/NAME") if n.text and n.text.strip()]
    if names:
        out["track_names"] = tuple(names)
    return {k: v for k, v in out.items() if v is not None}
