"""XML export: the sequence model's exact placement, both writers, and independent read-backs.

The xmeml files are also read back with OpenTimelineIO's FCP 7 XML adapter, and FCPXML with a small reader of the
format's timing rules (rational offsets relative to the parent gap), since OpenTimelineIO's FCPXML adapter truncates
NTSC rates. Golden files in ``fixtures/export`` catch unintended changes; regenerate them with
``MCSYNC_UPDATE_GOLDEN=1`` after checking the difference.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from mcsync.export import (
    ExportError,
    ExportOptions,
    build_sequence,
    export_timeline,
    file_url,
    format_report,
    write_fcpxml,
    write_xmeml,
)
from mcsync.export.sequence import ExportClip, _resolve_rounding_overlaps
from mcsync.export.xmeml import TICKS_PER_SECOND
from mcsync.media.probe import AudioStreamInfo, MediaInfo, TimecodeInfo, VideoStreamInfo
from mcsync.media.riff import BwfMetadata
from mcsync.project.db import ClipRow
from mcsync.sync.types import ClipPlacement, Flag, PlacementMethod, PlacementStatus
from mcsync.testing.nle import read_fcpxml, read_xmeml
from mcsync.timecode import Timecode, parse_frame_rate
from mcsync.timeline import Timeline, build_timeline

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "export"
NTSC_FILM = Fraction(24000, 1001)

# ---------------------------------------------------------------------------
# a small wedding, built by hand
# ---------------------------------------------------------------------------


def video_info(
    path: str, rate: str, duration: float, *, tc: str | None = None, channels: int = 2, vfr: bool = False
) -> MediaInfo:
    r = parse_frame_rate(rate)
    video = VideoStreamInfo(0, "h264", 1920, 1080, r, r, vfr, 0.0, duration)
    audio = (AudioStreamInfo(1, "aac", 48000, channels, "stereo", 0.0, duration),) if channels else ()
    timecode = None
    if tc:
        parsed = Timecode.parse(tc)
        timecode = TimecodeInfo(float(parsed.to_seconds(r)), r, parsed.drop_frame, "tmcd", tc)
    return MediaInfo(path, 1, 0, "mov", duration, (video,), audio, timecode=timecode)


def wav_info(path: str, duration: float, *, channels: int, time_reference: int, names: tuple[str, ...]) -> MediaInfo:
    audio = AudioStreamInfo(0, "pcm_s24le", 48000, channels, None, 0.0, duration, bits_per_sample=24)
    bwf = BwfMetadata(time_reference=time_reference, track_names=names)
    timecode = TimecodeInfo(time_reference / 48000, None, False, "bwf")
    return MediaInfo(path, 1, 0, "wav", duration, (), (audio,), timecode=timecode, bwf=bwf)


def clip_row(clip_id: int, info: MediaInfo, device: tuple[int, str, str], status: str = "online") -> ClipRow:
    device_id, name, kind = device
    return ClipRow(
        id=clip_id,
        name=info.path.replace("\\", "/").rsplit("/", 1)[-1],
        media_id=clip_id,
        path=info.path,
        fingerprint=f"fp{clip_id}",
        status=status,
        info=info,
        device_id=device_id,
        device_key=f"dev{device_id}",
        device_name=name,
        device_kind=kind,
        audio_stream=info.audio[0].index if info.audio else None,
        audio_channel=None,
        chapter_take=None,
        chapter_index=None,
        chapter_offset_s=None,
    )


ZOOM = (1, "ZOOM F8", "recorder")
CAM_A = (2, "Cam A", "camera")
CAM_B = (3, "Cam B", "camera")
GOPRO = (4, "GoPro", "camera")
DRONE = (5, "Drone", "drone")
PHONE = (6, "Phone", "phone")


def placement(clip_id: int, start: float | None, group: int | None = 0, **kw) -> ClipPlacement:
    status = kw.pop("status", PlacementStatus.SYNCED if start is not None else PlacementStatus.UNSYNCED)
    method = kw.pop("method", PlacementMethod.AUDIO if start is not None else PlacementMethod.NONE)
    return ClipPlacement(str(clip_id), start, group, method, kw.pop("confidence", 0.97), status, **kw)


@pytest.fixture
def wedding() -> tuple[Timeline, dict[int, ClipRow]]:
    rows = [
        clip_row(
            1,
            wav_info(
                "/Volumes/ZOOM F8/230614_001.WAV",
                600.0,
                channels=4,
                time_reference=36000 * 48000,
                names=("Mix L", "Mix R", "Lav Groom", "Lav Officiant"),
            ),
            ZOOM,
        ),  # fmt: skip
        clip_row(2, video_info("/Volumes/Card A/A001.MOV", "24000/1001", 120.0, tc="10:00:30:00"), CAM_A),
        clip_row(3, video_info("/Volumes/Card A/A002.MOV", "24000/1001", 200.0, tc="10:02:40:12"), CAM_A),
        clip_row(4, video_info(r"C:\Footage\Cam B\C0001 #2.MP4", "30000/1001", 150.0, tc="10:01:00;02"), CAM_B),
        clip_row(5, video_info("/Volumes/GoPro/GH010042.MP4", "50", 70.0), GOPRO),
        clip_row(6, video_info("/Volumes/GoPro/GH020042.MP4", "50", 60.0), GOPRO),
        clip_row(7, video_info("/Volumes/Drone/DJI_0001.MP4", "30", 40.0, tc="10:05:00:00", channels=0), DRONE),
        clip_row(8, video_info("/Volumes/Phone/IMG_0001.MOV", "30", 30.0), PHONE),  # detached group
        clip_row(9, video_info("/Volumes/Phone/IMG_0002.MOV", "30", 20.0), PHONE),  # not placed
        clip_row(10, video_info("/Volumes/Card A/A003.MOV", "24000/1001", 50.0), CAM_A, status="offline"),
    ]
    placements = {
        1: placement(1, 0.0, method=PlacementMethod.REFERENCE, confidence=1.0),
        2: placement(2, 30.0104),
        3: placement(3, 160.5, drift_ppm=12.0),
        4: placement(4, 60.2, status=PlacementStatus.NEEDS_REVIEW, confidence=0.5, flags=(Flag.AMBIGUOUS,)),
        5: placement(5, -10.0137),  # starts before the recorder: the recorder is trimmed to the frame grid
        6: placement(6, 59.9863, method=PlacementMethod.CHAPTER),
        7: placement(7, 300.0, method=PlacementMethod.TIMECODE),
        8: placement(8, 5.0, group=1),
        9: placement(9, None, group=None),
        10: placement(10, 400.0),
    }
    by_id = {r.id: r for r in rows}
    return build_timeline(rows, placements, reference_clip_id=1), by_id


def clip(seq, name: str) -> ExportClip:
    return next(c for c in seq.clips if c.name == name)


# ---------------------------------------------------------------------------
# the sequence model
# ---------------------------------------------------------------------------


def test_sequence_takes_the_dominant_rate_and_size(wedding):
    seq = build_sequence(*wedding)
    assert seq.rate == NTSC_FILM  # 320 s of 23.976 beats 150 s of 29.97 and 130 s of 50
    assert (seq.width, seq.height) == (1920, 1080)
    assert str(seq.start_timecode) == "01:00:00:00" and seq.start_frame == 86400
    assert build_sequence(*wedding, ExportOptions(sequence_rate=Fraction(25))).rate == 25


def test_tracks_follow_the_devices(wedding):
    seq = build_sequence(*wedding)
    assert [t.name for t in seq.video_tracks] == ["Cam A", "Cam B", "GoPro", "Drone"]
    assert [t.name for t in seq.audio_tracks] == [
        "Cam A 1", "Cam A 2", "Cam B 1", "Cam B 2", "GoPro 1", "GoPro 2",
        "ZOOM F8 Mix L", "ZOOM F8 Mix R", "ZOOM F8 Lav Groom", "ZOOM F8 Lav Officiant",
    ]  # fmt: skip
    assert clip(seq, "230614_001.WAV").audio_tracks == (7, 8, 9, 10) and clip(seq, "230614_001.WAV").video_track is None
    assert clip(seq, "DJI_0001.MP4").audio_tracks == () and clip(seq, "DJI_0001.MP4").video_track == 4


def test_clips_that_cannot_be_exported_are_listed(wedding):
    seq = build_sequence(*wedding)
    skipped = {s.name: s.reason for s in seq.skipped}
    assert skipped == {
        "IMG_0001.MOV": "not linked to the exported group (group 1)",
        "IMG_0002.MOV": "not placed",
        "A003.MOV": "media offline",
    }
    strict = build_sequence(*wedding, ExportOptions(include_uncertain=False))
    assert "C0001 #2.MP4" in {s.name for s in strict.skipped}
    assert "C0001 #2.MP4" not in {c.name for c in strict.clips}


def test_video_starts_on_the_nearest_frame(wedding):
    seq = build_sequence(*wedding)
    for c in seq.clips:
        if c.video_track is None:
            continue
        assert c.in_point == 0
        assert abs(c.error(seq.rate, subframe_in=True)) <= 1 / (2 * seq.rate)
    a001 = clip(seq, "A001.MOV")
    # GoPro starts 10.0137 s before the recorder, so the timeline origin is the GoPro: A001 is at 40.0241 s.
    assert a001.true_start == Fraction("40.0241")
    assert a001.start_frame == round(Fraction("40.0241") * NTSC_FILM) == 960
    assert a001.end_frame - a001.start_frame == 2877  # 120 s of 23.976 media


def test_audio_only_clips_are_placed_to_the_sample(wedding):
    seq = build_sequence(*wedding)
    zoom = clip(seq, "230614_001.WAV")
    assert zoom.true_start == Fraction("10.0137")
    assert Fraction(zoom.start_frame) / seq.rate >= zoom.true_start  # trimmed, never early
    assert zoom.start_frame == 241
    assert zoom.in_point == Fraction(round((Fraction(241) / NTSC_FILM - Fraction("10.0137")) * 48000), 48000)
    assert abs(zoom.error(seq.rate, subframe_in=True)) <= Fraction(1, 2 * 48000)
    assert abs(zoom.error(seq.rate, subframe_in=False)) <= 1 / (2 * seq.rate)  # readers that round the in point
    # The trimmed clip still ends inside the media.
    assert zoom.in_point + Fraction(zoom.duration_frames) / seq.rate <= Fraction(600)


def test_report_lists_errors_and_warnings(wedding):
    seq = build_sequence(*wedding)
    report = format_report(seq, "xmeml")
    assert report["sequence"]["rate"] == "24000/1001" and report["sequence"]["start_timecode"] == "01:00:00:00"
    assert report["max_error_ms"] <= 1000 / (2 * float(NTSC_FILM)) + 1e-9
    by_name = {c["name"]: c for c in report["clips"]}
    assert abs(by_name["230614_001.WAV"]["error_ms"]) > 0.1  # xmeml readers without ticks round the in point
    assert format_report(seq, "fcpxml")["clips"][0]["name"] == "GH010042.MP4"
    assert abs({c["name"]: c for c in format_report(seq, "fcpxml")["clips"]}["230614_001.WAV"]["error_ms"]) < 0.011
    warnings = " ".join(report["warnings"])
    assert "C0001 #2.MP4: placed by an uncertain audio match" in warnings
    assert "A002.MOV: its clock drifts 12.0 ppm" not in warnings  # 1.2 ms at the ends: below half a frame


def test_drift_beyond_half_a_frame_is_reported(wedding):
    timeline, rows = wedding
    drifting = Timeline(
        tuple(
            g.__class__(
                **{
                    **g.__dict__,
                    "clips": tuple(
                        c.__class__(**{**c.__dict__, "drift_ppm": 150.0}) if c.name == "A002.MOV" else c
                        for c in g.clips
                    ),
                }
            )
            for g in timeline.groups
        ),  # fmt: skip
        timeline.unsynced,
        timeline.review,
        timeline.reference_clip_id,
        timeline.stats,
    )
    seq = build_sequence(drifting, rows, ExportOptions(sequence_rate=Fraction(50)))
    assert any(w.startswith("A002.MOV: its clock drifts 150.0 ppm") for w in seq.warnings)


def test_start_timecode_options(wedding):
    df = build_sequence(*wedding, ExportOptions(sequence_rate=Fraction(30000, 1001), start_timecode="01:00:00;00"))
    assert df.drop_frame and df.start_frame == 107892 and str(df.start_timecode) == "01:00:00;00"
    ndf = build_sequence(*wedding, ExportOptions(sequence_rate=Fraction(25), start_timecode="00:59:58;00"))
    assert not ndf.drop_frame and ndf.start_frame == (3598 * 25)
    with pytest.raises(ExportError, match="invalid start timecode"):
        build_sequence(*wedding, ExportOptions(start_timecode="1 hour"))


def test_nothing_to_export():
    with pytest.raises(ExportError, match="synchronise first"):
        build_sequence(Timeline((), (), ()), {})


def test_rounding_overlaps_are_trimmed(wedding):
    seq = build_sequence(*wedding)
    a, b = clip(seq, "GH010042.MP4"), clip(seq, "GH020042.MP4")
    assert a.end_frame <= b.start_frame  # back-to-back chapters never overlap
    overlapping = [a.__class__(**{**a.__dict__, "end_frame": b.start_frame + 1}), b]
    warnings: list[str] = []
    fixed = {c.name: c for c in _resolve_rounding_overlaps(overlapping, warnings)}
    assert fixed["GH010042.MP4"].end_frame == b.start_frame and not warnings
    far = [a.__class__(**{**a.__dict__, "end_frame": b.start_frame + 10}), b]
    _resolve_rounding_overlaps(far, warnings)
    assert warnings == ["GH010042.MP4 and GH020042.MP4 overlap on one track by 10 frames."]


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "localhost", "url"),
    [
        ("/Volumes/Card A/A001.MOV", True, "file://localhost/Volumes/Card%20A/A001.MOV"),
        ("/Volumes/Card A/A001.MOV", False, "file:///Volumes/Card%20A/A001.MOV"),
        ("/media/Hochzeit Müller/#1?.wav", False, "file:///media/Hochzeit%20M%C3%BCller/%231%3F.wav"),
        (r"C:\Footage\Cam B\C0001 #2.MP4", True, "file://localhost/C:/Footage/Cam%20B/C0001%20%232.MP4"),
        (r"\\nas\shoots\2026\A001.MOV", False, "file://nas/shoots/2026/A001.MOV"),
    ],
)
def test_file_urls(path, localhost, url):
    assert file_url(path, localhost=localhost) == url


def test_relative_paths_are_rejected():
    with pytest.raises(ValueError, match="absolute"):
        file_url("footage/A001.MOV")


# ---------------------------------------------------------------------------
# xmeml
# ---------------------------------------------------------------------------


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("MCSYNC_UPDATE_GOLDEN") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{name} changed; set MCSYNC_UPDATE_GOLDEN=1 if intended"


def test_xmeml_matches_the_golden_file(wedding):
    _golden("wedding.xml", write_xmeml(build_sequence(*wedding, ExportOptions(name="Smith wedding"))))


def test_xmeml_places_every_clip_item(wedding):
    seq = build_sequence(*wedding)
    root = ET.fromstring(write_xmeml(seq))
    sequence = root.find("sequence")
    assert root.get("version") == "5"
    assert sequence.findtext("rate/timebase") == "24" and sequence.findtext("rate/ntsc") == "TRUE"
    assert sequence.findtext("timecode/string") == "01:00:00:00" and sequence.findtext("timecode/frame") == "86400"
    assert int(sequence.findtext("duration")) == seq.duration_frames

    video_tracks = sequence.findall("media/video/track")
    audio_tracks = sequence.findall("media/audio/track")
    assert len(video_tracks) == 4 and len(audio_tracks) == 10
    items = {i.get("id"): i for i in sequence.iter("clipitem")}
    for c in seq.clips:
        own = [i for i in items.values() if i.findtext("name") == c.name]
        assert len(own) == (1 if c.video_track else 0) + len(c.audio_tracks)
        for item in own:
            assert int(item.findtext("start")) == c.start_frame and int(item.findtext("end")) == c.end_frame
            assert int(item.findtext("out")) - int(item.findtext("in")) == c.duration_frames
            assert Fraction(int(item.findtext("pproTicksIn")), TICKS_PER_SECOND) == c.in_point
            links = item.findall("link")
            assert len(links) == (len(own) if len(own) > 1 else 0)
            for link in links:
                target = items[link.findtext("linkclipref")]
                assert target.findtext("name") == c.name
                kind = link.findtext("mediatype")
                track = (video_tracks if kind == "video" else audio_tracks)[int(link.findtext("trackindex")) - 1]
                assert track.findall("clipitem")[int(link.findtext("clipindex")) - 1] is target

    # Each file is described once, then referenced by id.
    files = list(sequence.iter("file"))
    full = [f for f in files if len(f)]
    assert sorted(f.get("id") for f in full) == sorted(m.key for m in seq.media)
    assert {f.get("id") for f in files} == {m.key for m in seq.media}
    # Every item of a clip puts its first frame at the same place: exact with ticks, within half a frame without.
    for name, positions in read_xmeml(write_xmeml(seq)).items():
        c = clip(seq, name)
        assert positions == {c.placed_start(seq.rate, subframe_in=True)}
        (rounded,) = read_xmeml(write_xmeml(seq), ticks=False)[name]
        assert abs(rounded - c.true_start) <= 1 / (2 * seq.rate)
    cam_b = next(f for f in full if f.findtext("name") == "C0001 #2.MP4")
    assert cam_b.findtext("pathurl") == "file://localhost/C:/Footage/Cam%20B/C0001%20%232.MP4"
    assert cam_b.findtext("timecode/string") == "10:01:00;02" and cam_b.findtext("timecode/displayformat") == "DF"
    assert cam_b.findtext("rate/timebase") == "30" and cam_b.findtext("media/audio/channelcount") == "2"


def test_xmeml_reads_back_with_opentimelineio(wedding, tmp_path):
    otio = pytest.importorskip("opentimelineio")
    if "fcp_xml" not in otio.adapters.available_adapter_names():
        pytest.skip("the FCP 7 XML adapter (otio-fcp-adapter) is not installed")
    seq = build_sequence(*wedding)
    path = tmp_path / "wedding.xml"
    path.write_text(write_xmeml(seq), encoding="utf-8")
    timeline = otio.adapters.read_from_file(str(path), adapter_name="fcp_xml")
    placed: dict[str, set[tuple[int, int]]] = {}
    for track in timeline.tracks:
        for item in track.find_clips():
            r = item.range_in_parent()
            frames = (round(r.start_time.to_seconds() * seq.rate), round(r.duration.to_seconds() * seq.rate))
            placed.setdefault(item.name, set()).add(frames)
    for c in seq.clips:
        assert placed[c.name] == {(c.start_frame, c.duration_frames)}, c.name


# ---------------------------------------------------------------------------
# FCPXML
# ---------------------------------------------------------------------------


def _t(value: str) -> Fraction:
    assert value.endswith("s"), value
    return Fraction(value[:-1])


def test_fcpxml_matches_the_golden_file(wedding):
    _golden("wedding.fcpxml", write_fcpxml(build_sequence(*wedding, ExportOptions(name="Smith wedding"))))


def test_fcpxml_places_every_clip_exactly(wedding):
    seq = build_sequence(*wedding)
    text = write_fcpxml(seq)
    root = ET.fromstring(text)
    assert root.get("version") == "1.10"
    sequence = root.find("library/event/project/sequence")
    assert _t(sequence.get("tcStart")) == Fraction(86400) / NTSC_FILM and sequence.get("tcFormat") == "NDF"
    placed = read_fcpxml(text)
    assert set(placed) == {c.name for c in seq.clips}
    for c in seq.clips:
        p = placed[c.name]
        assert p["position"] == Fraction(c.start_frame) / seq.rate, c.name
        assert p["in"] == c.in_point, c.name
        assert p["duration"] == Fraction(c.duration_frames) / seq.rate, c.name
        assert (p["position"] * seq.rate).denominator == 1  # on the sequence's frame grid
        assert p["lane"] == (c.video_track if c.video_track else -1)
    zoom = next(c for c in seq.clips if c.name == "230614_001.WAV")
    assert placed["230614_001.WAV"]["position"] - placed["230614_001.WAV"]["in"] == zoom.true_start + zoom.error(
        seq.rate, subframe_in=True
    )
    assets = {a.get("name"): a for a in root.iter("asset")}
    assert _t(assets["230614_001.WAV"].get("start")) == 36000  # the BWF time reference, to the sample
    assert _t(assets["A001.MOV"].get("start")) == Timecode.parse("10:00:30:00").to_seconds(NTSC_FILM)
    assert placed["C0001 #2.MP4"]["src"] == "file:///C:/Footage/Cam%20B/C0001%20%232.MP4"
    # Resource ids run in document order.
    ids = [int(e.get("id")[1:]) for e in root.find("resources")]
    assert ids == sorted(ids)


def test_fcpxml_reads_back_with_opentimelineio_at_integer_rates(tmp_path):
    otio = pytest.importorskip("opentimelineio")
    if "fcpx_xml" not in otio.adapters.available_adapter_names():
        pytest.skip("the FCPXML adapter (otio-fcpx-xml-adapter) is not installed")
    rows = [
        clip_row(1, wav_info("/rec/ZOOM0001.WAV", 300.0, channels=2, time_reference=0, names=()), ZOOM),
        clip_row(2, video_info("/cards/A/A001.MP4", "25", 100.0), CAM_A),
        clip_row(3, video_info("/cards/B/B001.MP4", "25", 80.0), CAM_B),
    ]
    placements = {1: placement(1, 0.0, method=PlacementMethod.REFERENCE), 2: placement(2, 12.0), 3: placement(3, 40.4)}
    seq = build_sequence(build_timeline(rows, placements, 1), {r.id: r for r in rows})
    path = tmp_path / "simple.fcpxml"
    path.write_text(write_fcpxml(seq), encoding="utf-8")
    timeline = otio.adapters.read_from_file(str(path), adapter_name="fcpx_xml")
    if isinstance(timeline, otio.schema.SerializableCollection):
        timeline = next(iter(timeline))
    starts = {c.name: c.range_in_parent().start_time.to_seconds() for c in timeline.find_clips()}
    origin = Fraction(seq.start_frame) / seq.rate
    for c in seq.clips:
        assert starts[c.name] == pytest.approx(float(origin + Fraction(c.start_frame) / seq.rate)), c.name


# ---------------------------------------------------------------------------
# writing files
# ---------------------------------------------------------------------------


def test_export_writes_atomically_and_reports(wedding, tmp_path):
    out = tmp_path / "exports" / "wedding.fcpxml"
    report = export_timeline(*wedding, "fcpxml", out)
    assert out.read_text(encoding="utf-8").startswith('<?xml version="1.0"')
    assert report["path"] == str(out) and report["format"] == "fcpxml" and len(report["clips"]) == 7
    assert [p.name for p in out.parent.iterdir()] == ["wedding.fcpxml"]  # no temporary files left behind
    if os.name == "posix":  # readable like any document, not private like a temporary file
        umask = os.umask(0)
        os.umask(umask)
        assert out.stat().st_mode & 0o777 == 0o666 & ~umask
    with pytest.raises(ExportError, match="unknown export format"):
        export_timeline(*wedding, "edl", tmp_path / "x.edl")
