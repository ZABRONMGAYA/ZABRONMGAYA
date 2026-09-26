"""Minimal readers of exported XML timelines, for tests.

Each applies its format's timing rules exactly (rational seconds), independently of the writers, and returns where
every clip's first frame (or first sample) lands on the sequence, counted from the sequence's first frame.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

TICKS_PER_SECOND = 254_016_000_000


def _rate(element: ET.Element) -> Fraction:
    timebase = int(element.findtext("rate/timebase"))  # type: ignore[arg-type]
    ntsc = element.findtext("rate/ntsc") == "TRUE"
    return Fraction(timebase * 1000, 1001) if ntsc else Fraction(timebase)


def read_xmeml(text: str, *, ticks: bool = True) -> dict[str, set[Fraction]]:
    """Clip name → positions of its first frame, one per clip item (video and each audio channel).

    ``ticks=False`` reads the in point from ``<in>`` frames, as NLEs that ignore Premiere's ticks do.
    """
    sequence = ET.fromstring(text).find("sequence")
    assert sequence is not None
    rate = _rate(sequence)
    out: dict[str, set[Fraction]] = {}
    for item in sequence.iter("clipitem"):
        item_rate = _rate(item)
        start = Fraction(int(item.findtext("start"))) / rate  # type: ignore[arg-type]
        if ticks and item.findtext("pproTicksIn") is not None:
            in_point = Fraction(int(item.findtext("pproTicksIn")), TICKS_PER_SECOND)  # type: ignore[arg-type]
        else:
            in_point = Fraction(int(item.findtext("in"))) / item_rate  # type: ignore[arg-type]
        out.setdefault(item.findtext("name") or "", set()).add(start - in_point)
    return out


def _t(value: str) -> Fraction:
    if not value.endswith("s"):
        raise ValueError(f"not an FCPXML time: {value!r}")
    return Fraction(value[:-1])


def read_fcpxml(text: str) -> dict[str, dict]:
    """Clip name → its position, source in point, duration, lane and media URL, by FCPXML's timing rules."""
    root = ET.fromstring(text)
    assets = {a.get("id"): a for a in root.iter("asset")}
    sequence = root.find("library/event/project/sequence")
    assert sequence is not None
    tc_start = _t(sequence.get("tcStart", "0s"))
    out = {}
    for gap in sequence.iter("gap"):
        for c in gap.findall("asset-clip"):
            # A connected clip's offset is in its parent's local time, which begins at the parent's start.
            position = _t(gap.get("offset", "0s")) + _t(c.get("offset", "0s")) - _t(gap.get("start", "0s")) - tc_start
            asset = assets[c.get("ref")]
            in_point = _t(c.get("start", "0s")) - _t(asset.get("start", "0s"))
            media_rep = asset.find("media-rep")
            out[c.get("name")] = {
                "position": position,
                "in": in_point,
                "first_sample": position - in_point,
                "duration": _t(c.get("duration", "0s")),
                "lane": int(c.get("lane", "0")),
                "src": media_rep.get("src") if media_rep is not None else None,
            }
    return out
