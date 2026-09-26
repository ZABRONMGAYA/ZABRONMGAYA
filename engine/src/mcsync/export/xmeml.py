"""Final Cut Pro 7 XML (xmeml version 5): imported by Premiere Pro and DaVinci Resolve.

Conventions, as Premiere Pro writes them for mixed-rate sequences:

* ``<start>``/``<end>`` count sequence frames from the sequence's first frame (not from its timecode).
* A clip item's ``<rate>`` is the sequence rate, and its ``<in>``/``<out>``/``<duration>`` are sequence frames
  counted from the file's first frame. The ``<file>`` keeps the media's own rate and length.
* ``<pproTicksIn>``/``<pproTicksOut>`` give Premiere the in point to 1/254 016 000 000 s, which places trimmed
  audio-only clips to the sample. Readers that ignore them round the in point to a frame.
* A file is described in full at its first reference and by ``<file id="…"/>`` after that.
* Camera audio sits on its own tracks, one per channel, linked to the video.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction

from mcsync.timecode import Timecode, nominal_fps

from .sequence import ExportClip, ExportSequence, SourceMedia, media_frames
from .urls import file_url

TICKS_PER_SECOND = 254_016_000_000


def _el(parent: ET.Element, tag: str, text: object = None, **attrs: str) -> ET.Element:
    e = ET.SubElement(parent, tag, attrs)
    if text is not None:
        e.text = str(text)
    return e


def _bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _rate(parent: ET.Element, rate: Fraction) -> None:
    r = _el(parent, "rate")
    _el(r, "timebase", nominal_fps(rate))
    _el(r, "ntsc", _bool(rate.denominator == 1001))


def _timecode(parent: ET.Element, rate: Fraction, frames: int, drop_frame: bool) -> None:
    tc = _el(parent, "timecode")
    _rate(tc, rate)
    _el(tc, "string", Timecode.from_frames(frames, rate, drop_frame))
    _el(tc, "frame", frames)
    _el(tc, "displayformat", "DF" if drop_frame else "NDF")


def _video_characteristics(parent: ET.Element, rate: Fraction, width: int, height: int) -> None:
    sc = _el(parent, "samplecharacteristics")
    _rate(sc, rate)
    _el(sc, "width", width)
    _el(sc, "height", height)
    _el(sc, "anamorphic", "FALSE")
    _el(sc, "pixelaspectratio", "square")
    _el(sc, "fielddominance", "none")


def _audio_characteristics(parent: ET.Element, depth: int, sample_rate: int) -> None:
    sc = _el(parent, "samplecharacteristics")
    _el(sc, "depth", depth)
    _el(sc, "samplerate", sample_rate)


def _ticks(seconds: Fraction) -> int:
    return round(seconds * TICKS_PER_SECOND)


class _Writer:
    def __init__(self, seq: ExportSequence) -> None:
        self.seq = seq
        self.described: set[str] = set()
        self.masterclips = {m.key: f"masterclip-{i + 1}" for i, m in enumerate(seq.media)}
        # clipitem ids in document order: video tracks, then audio tracks.
        self.items: dict[tuple[int, str, int], str] = {}  # (clip_id, kind, track) -> id
        self.index_in_track: dict[tuple[int, str, int], int] = {}
        self.video = {t.index: self._on_track("video", t.index) for t in seq.video_tracks}
        self.audio = {t.index: self._on_track("audio", t.index) for t in seq.audio_tracks}

    def _on_track(self, kind: str, track: int) -> list[ExportClip]:
        clips = [
            c
            for c in self.seq.clips
            if (kind == "video" and c.video_track == track) or (kind == "audio" and track in c.audio_tracks)
        ]
        clips.sort(key=lambda c: (c.start_frame, c.clip_id))
        for i, c in enumerate(clips):
            key = (c.clip_id, kind, track)
            self.items[key] = f"clipitem-{len(self.items) + 1}"
            self.index_in_track[key] = i + 1
        return clips

    def file(self, parent: ET.Element, media: SourceMedia) -> None:
        if media.key in self.described:
            _el(parent, "file", id=media.key)
            return
        self.described.add(media.key)
        seq = self.seq
        f = _el(parent, "file", id=media.key)
        _el(f, "name", media.name)
        _el(f, "pathurl", file_url(media.path, localhost=True))
        rate = media.video_rate or seq.rate
        _rate(f, rate)
        _el(f, "duration", media_frames(media, rate))
        if media.timecode_frames is not None and media.timecode_rate is not None:
            _timecode(f, media.timecode_rate, media.timecode_frames, media.drop_frame)
        m = _el(f, "media")
        if media.has_video:
            _video_characteristics(_el(m, "video"), rate, media.width or seq.width, media.height or seq.height)
        if media.audio_channels:
            a = _el(m, "audio")
            _audio_characteristics(a, media.bit_depth, media.sample_rate)
            _el(a, "channelcount", media.audio_channels)

    def clipitem(self, parent: ET.Element, clip: ExportClip, kind: str, track: int) -> None:
        seq = self.seq
        item = _el(parent, "clipitem", id=self.items[(clip.clip_id, kind, track)])
        _el(item, "masterclipid", self.masterclips[clip.media.key])
        _el(item, "name", clip.name)
        _el(item, "enabled", "TRUE")
        _el(item, "duration", media_frames(clip.media, seq.rate))
        _rate(item, seq.rate)
        in_frames = round(clip.in_point * seq.rate)
        _el(item, "start", clip.start_frame)
        _el(item, "end", clip.end_frame)
        _el(item, "in", in_frames)
        _el(item, "out", in_frames + clip.duration_frames)
        _el(item, "pproTicksIn", _ticks(clip.in_point))
        _el(item, "pproTicksOut", _ticks(clip.in_point + Fraction(clip.duration_frames) / seq.rate))
        if kind == "video":
            _el(item, "alphatype", "none")
        self.file(item, clip.media)
        source = _el(item, "sourcetrack")
        _el(source, "mediatype", kind)
        _el(source, "trackindex", 1 if kind == "video" else clip.audio_tracks.index(track) + 1)
        parts = ([("video", clip.video_track)] if clip.video_track else []) + [("audio", t) for t in clip.audio_tracks]
        if len(parts) > 1:
            for other_kind, other_track in parts:
                link = _el(item, "link")
                key = (clip.clip_id, other_kind, other_track)
                _el(link, "linkclipref", self.items[key])
                _el(link, "mediatype", other_kind)
                _el(link, "trackindex", other_track)
                _el(link, "clipindex", self.index_in_track[key])
                if other_kind == "audio":
                    _el(link, "groupindex", 1)

    def write(self) -> ET.Element:
        seq = self.seq
        root = ET.Element("xmeml", version="5")
        s = _el(root, "sequence", id="sequence-1")
        _el(s, "name", seq.name)
        _el(s, "duration", seq.duration_frames)
        _rate(s, seq.rate)
        _timecode(s, seq.rate, seq.start_frame, seq.drop_frame)
        media = _el(s, "media")

        video = _el(media, "video")
        _video_characteristics(_el(video, "format"), seq.rate, seq.width, seq.height)
        for t in seq.video_tracks:
            track = _el(video, "track")
            for clip in self.video[t.index]:
                self.clipitem(track, clip, "video", t.index)
            _el(track, "enabled", "TRUE")
            _el(track, "locked", "FALSE")

        audio = _el(media, "audio")
        _el(audio, "numOutputChannels", 2)
        _audio_characteristics(_el(audio, "format"), 16, seq.sample_rate)
        for t in seq.audio_tracks:
            track = _el(audio, "track")
            for clip in self.audio[t.index]:
                self.clipitem(track, clip, "audio", t.index)
            _el(track, "enabled", "TRUE")
            _el(track, "locked", "FALSE")
            _el(track, "outputchannelindex", 2 if t.channel and t.channel % 2 == 0 else 1)
        return root


def write_xmeml(seq: ExportSequence) -> str:
    root = _Writer(seq).write()
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n{body}\n'
