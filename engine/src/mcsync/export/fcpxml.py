"""FCPXML 1.10: imported by DaVinci Resolve (and Final Cut Pro), with rational, sample-accurate times.

Structure: formats and assets (one per file, with its own start: timecode or BWF time reference) under
``<resources>``; the sequence's spine holds one gap spanning the timeline, and every clip is connected to it:
cameras on lanes 1, 2, … (with their own audio), recorders on lanes −1, −2, ….

A clip's ``offset`` is on the sequence's frame grid; its ``start`` is the asset start plus the in point, which for
audio-only clips carries the sub-frame part of the synchronised position (to the sample).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

from .sequence import ExportSequence, SourceMedia, media_frames
from .urls import file_url

VERSION = "1.10"


def fcp_time(t: Fraction, rate: Fraction | int | None = None) -> str:
    """``"3600s"``, ``"1001/24000s"``, ``"1824/48000s"``: a time on the grid of ``rate`` (frames or samples per
    second) is written over that grid's denominator, as Final Cut Pro writes it."""
    rate = Fraction(rate) if rate is not None else None
    if t.denominator == 1:
        return f"{t.numerator}s"
    if rate is not None and (t * rate).denominator == 1:
        frames = int(t * rate)
        return f"{frames * rate.denominator}/{rate.numerator}s"
    return f"{t.numerator}/{t.denominator}s"


def _el(parent: ET.Element, tag: str, **attrs: str) -> ET.Element:
    return ET.SubElement(parent, tag, attrs)


class _Writer:
    def __init__(self, seq: ExportSequence) -> None:
        self.seq = seq
        self.ids = 0
        self.formats: dict[tuple[Fraction, int, int], str] = {}
        self.assets: dict[str, str] = {}

    def _id(self) -> str:
        self.ids += 1
        return f"r{self.ids}"

    def format(self, resources: ET.Element, rate: Fraction, width: int, height: int) -> str:
        key = (rate, width, height)
        if key not in self.formats:
            self.formats[key] = self._id()
            _el(
                resources,
                "format",
                id=self.formats[key],
                frameDuration=fcp_time(1 / rate),
                width=str(width),
                height=str(height),
            )
        return self.formats[key]

    def asset(self, resources: ET.Element, media: SourceMedia) -> None:
        seq = self.seq
        rate = media.video_rate
        # The asset's format first, so ids run in document order.
        fmt = self.format(resources, rate, media.width or seq.width, media.height or seq.height) if rate else None
        attrs = {"id": self._id(), "name": media.name, "start": ""}
        if rate is not None and fmt is not None:
            attrs["start"] = fcp_time(media.start, rate)
            attrs["duration"] = fcp_time(Fraction(media_frames(media, rate)) / rate, rate)
            attrs |= {"hasVideo": "1", "format": fmt, "videoSources": "1"}
        else:
            attrs["start"] = fcp_time(media.start, media.sample_rate)
            samples = Fraction(round(media.duration * media.sample_rate), media.sample_rate)
            attrs["duration"] = fcp_time(samples, media.sample_rate)
        if media.audio_channels:
            attrs |= {
                "hasAudio": "1",
                "audioSources": "1",
                "audioChannels": str(media.audio_channels),
                "audioRate": str(media.sample_rate),
            }
        asset = _el(resources, "asset", **attrs)
        _el(asset, "media-rep", kind="original-media", src=file_url(media.path))
        self.assets[media.key] = attrs["id"]

    def write(self) -> ET.Element:
        seq = self.seq
        rate = seq.rate
        root = ET.Element("fcpxml", version=VERSION)
        resources = _el(root, "resources")
        seq_format = self.format(resources, rate, seq.width, seq.height)
        for media in seq.media:
            self.asset(resources, media)

        library = _el(root, "library")
        event = _el(library, "event", name="Multicam Sync")
        project = _el(event, "project", name=seq.name)
        tc_start = Fraction(seq.start_frame) / rate
        duration = Fraction(seq.duration_frames) / rate
        sequence = _el(
            project,
            "sequence",
            format=seq_format,
            duration=fcp_time(duration, rate),
            tcStart=fcp_time(tc_start, rate),
            tcFormat="DF" if seq.drop_frame else "NDF",
            audioLayout="stereo",
            audioRate="48k" if seq.sample_rate == 48_000 else "44.1k",
        )
        spine = _el(sequence, "spine")
        gap = _el(
            spine,
            "gap",
            name="Gap",
            offset=fcp_time(tc_start, rate),
            start=fcp_time(tc_start, rate),
            duration=fcp_time(duration, rate),
        )

        # Recorder lanes below the spine, in audio-track order.
        audio_only = sorted({c.audio_tracks[0] for c in seq.clips if c.video_track is None and c.audio_tracks})
        audio_lane = {track: -(i + 1) for i, track in enumerate(audio_only)}
        for clip in seq.clips:
            media = clip.media
            if clip.video_track is not None:
                lane = clip.video_track
            elif clip.audio_tracks:
                lane = audio_lane[clip.audio_tracks[0]]
            else:
                continue
            grid = media.video_rate if clip.video_track else media.sample_rate
            attrs = {
                "ref": self.assets[media.key],
                "lane": str(lane),
                "offset": fcp_time(tc_start + Fraction(clip.start_frame) / rate, rate),
                "name": clip.name,
                "start": fcp_time(media.start + clip.in_point, grid),
                "duration": fcp_time(Fraction(clip.duration_frames) / rate, rate),
                "tcFormat": "DF" if media.drop_frame else "NDF",
            }
            if media.audio_channels:
                attrs["audioRole"] = "dialogue"
            _el(gap, "asset-clip", **attrs)
        return root


def write_fcpxml(seq: ExportSequence) -> str:
    root = _Writer(seq).write()
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{body}\n'
