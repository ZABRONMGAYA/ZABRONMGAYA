"""Media-layer logic that needs no FFmpeg: metadata parsing, BWF chunks, devices, peaks, cache."""

from __future__ import annotations

import os
import struct
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from mcsync.media.cache import AnalysisCache, params_key
from mcsync.media.devices import card_root, file_family, find_chapters, identify_device, is_record_run
from mcsync.media.fingerprint import fingerprint
from mcsync.media.probe import MediaInfo, ProbeError, TimecodeInfo, parse_probe, parse_rate, read_sidecar
from mcsync.media.riff import read_bwf
from mcsync.media.waveform import PEAK_LEVELS, PeakBuilder, decode_peaks, read_peaks, write_peaks
from mcsync.sync import SyncParams

# ---------------------------------------------------------------------------
# ffprobe JSON
# ---------------------------------------------------------------------------


def video(index=0, rate="24000/1001", avg=None, start="0.000000", duration="10.010000", tags=None, **kw):
    return {
        "index": index,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 3840,
        "height": 2160,
        "r_frame_rate": rate,
        "avg_frame_rate": avg or rate,
        "start_time": start,
        "duration": duration,
        "tags": tags or {},
        **kw,
    }


def audio(index=1, start="0.000000", duration="10.000000", channels=2, **kw):
    return {
        "index": index,
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": channels,
        "channel_layout": "stereo",
        "start_time": start,
        "duration": duration,
        **kw,
    }


def fmt(tags=None, duration="10.010000", name="mov,mp4,m4a,3gp,3g2,mj2"):
    return {"format_name": name, "duration": duration, "tags": tags or {}}


def parse(streams, format_=None, **kw):
    return parse_probe({"streams": streams, "format": format_ or fmt()}, path="/card/DCIM/100CANON/MVI_0001.MP4", **kw)


def test_quicktime_camera_with_tmcd_track():
    tmcd = {"index": 2, "codec_type": "data", "codec_tag_string": "tmcd", "avg_frame_rate": "24000/1001",
            "tags": {"timecode": "01:00:00:00"}}  # fmt: skip
    info = parse([video(), audio(), tmcd], fmt({"creation_time": "2026-06-14T15:04:05.000000Z"}))
    assert info.frame_rate == Fraction(24000, 1001)
    assert info.duration_s == pytest.approx(10.01)
    tc = info.timecode
    assert (tc.source, tc.text, tc.drop_frame, tc.family) == ("tmcd", "01:00:00:00", False, "ntsc")
    assert tc.seconds == pytest.approx(3603.6)
    assert info.creation_time == datetime(2026, 6, 14, 15, 4, 5, tzinfo=UTC)
    assert info.audio_start_s() == 0.0
    assert not info.is_audio_only


def test_drop_frame_timecode_from_video_tags_and_container_tags():
    info = parse([video(rate="30000/1001", tags={"timecode": "10:00:00;02"}), audio()])
    assert (info.timecode.source, info.timecode.drop_frame, info.timecode.family) == ("video", True, "wall")
    assert info.timecode.seconds == pytest.approx(36000.0307, abs=1e-4)
    mxf = parse([video(rate="25/1"), audio()], fmt({"timecode": "12:34:56:07"}, name="mxf"))
    assert mxf.timecode.source == "container"
    assert mxf.timecode.seconds == pytest.approx(45296.28)


def test_invalid_timecode_is_ignored():
    info = parse([video(rate="25/1", tags={"timecode": "00:00:00:30"}), audio()])  # frame 30 at 25 fps
    assert info.timecode is None


def test_mpegts_stream_offsets_become_audio_start():
    info = parse([video(rate="50/1", start="1.405333"), audio(start="1.400000")], fmt(name="mpegts"))
    assert info.origin_s == pytest.approx(1.405333)
    assert info.audio_start_s() == pytest.approx(-0.005333)


def test_variable_frame_rate_phone_footage():
    info = parse([video(rate="30/1", avg="29500/1000"), audio()])
    assert info.video[0].is_vfr
    timebase_like = parse([video(rate="600/1", avg="2997/100"), audio()])
    assert timebase_like.frame_rate == Fraction(30000, 1001)  # snapped, not 600 fps


def test_rates_snap_to_standard_values():
    assert parse_rate("2997/100") == Fraction(30000, 1001)
    assert parse_rate("24000/1001") == Fraction(24000, 1001)
    assert parse_rate("25") == 25
    assert parse_rate("0/0") is None
    assert parse_rate(None) is None
    assert parse_rate("12/1") == 12


def test_cover_art_and_empty_streams_are_skipped():
    cover = video(index=1, codec_name="mjpeg", disposition={"attached_pic": 1})
    info = parse([audio(index=0), cover], fmt(name="mp3"))
    assert info.is_audio_only and info.frame_rate is None
    with pytest.raises(ProbeError):
        parse([{"index": 0, "codec_type": "subtitle"}])
    with pytest.raises(ProbeError):
        parse([audio(duration=None)], fmt(duration=None))


def test_creation_time_variants():
    apple = parse([video(), audio()], fmt({"com.apple.quicktime.creationdate": "2026-06-14T17:04:05+0200",
                                           "creation_time": "2026-06-14T15:04:09.000000Z"}))  # fmt: skip
    assert apple.creation_time == datetime(2026, 6, 14, 15, 4, 5, tzinfo=UTC)
    unset = parse([video(), audio()], fmt({"creation_time": "1904-01-01T00:00:00.000000Z"}))
    assert unset.creation_time is None
    naive = parse([video(), audio()], fmt({"creation_time": "2026-06-14 15:04:05"}))
    assert naive.creation_time == datetime(2026, 6, 14, 15, 4, 5, tzinfo=UTC)


def test_device_fields():
    iphone = parse(
        [video(), audio()], fmt({"com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 16"})
    )
    assert (iphone.make, iphone.model) == ("Apple", "iPhone 16")
    gopro = parse([video(tags={"handler_name": "GoPro AVC  "}), audio()])
    assert gopro.make == "GoPro"
    sony = parse([video(), audio()], sidecar={"make": "Sony", "model": "ILCE-7M4", "serial": "1234567"})
    assert (sony.make, sony.model, sony.serial) == ("Sony", "ILCE-7M4", "1234567")
    mxf = parse([video(), audio()], fmt({"company_name": "Sony", "product_name": "PXW-FS7"}, name="mxf"))
    assert (mxf.make, mxf.model) == ("Sony", "PXW-FS7")


def test_sony_sidecar(tmp_path):
    clip = tmp_path / "C0001.MP4"
    clip.write_bytes(b"")
    (tmp_path / "C0001M01.XML").write_text(
        '<?xml version="1.0"?><NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">'
        '<Device manufacturer="Sony" modelName="ILCE-7SM3" serialNo="4001234"/></NonRealTimeMeta>'
    )
    assert read_sidecar(clip) == {"make": "Sony", "model": "ILCE-7SM3", "serial": "4001234"}
    assert read_sidecar(tmp_path / "C0002.MP4") == {}


# ---------------------------------------------------------------------------
# Broadcast WAV chunks
# ---------------------------------------------------------------------------


def chunk(cid: bytes, body: bytes) -> bytes:
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def bext(time_reference: int, originator=b"Sound Devices", reference=b"888") -> bytes:
    body = bytearray(602)
    body[256 : 256 + len(originator)] = originator
    body[288 : 288 + len(reference)] = reference
    body[338:346] = struct.pack("<II", time_reference & 0xFFFFFFFF, time_reference >> 32)
    return bytes(body)


IXML = b"""<?xml version="1.0"?><BWFXML><PROJECT>Smith Wedding</PROJECT><SCENE>Ceremony</SCENE><TAKE>3</TAKE>
<SPEED><TIMECODE_RATE>24000/1001</TIMECODE_RATE><TIMECODE_FLAG>NDF</TIMECODE_FLAG>
<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>0</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>
<TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>999</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO></SPEED>
<TRACK_LIST><TRACK><NAME>Groom lav</NAME></TRACK><TRACK><NAME>Officiant</NAME></TRACK></TRACK_LIST></BWFXML>"""
FMT = struct.pack("<HHIIHH", 1, 2, 48000, 48000 * 6, 6, 24)


def test_bwf_bext_and_ixml(tmp_path):
    path = tmp_path / "take3.wav"
    body = b"WAVE" + chunk(b"fmt ", FMT) + chunk(b"bext", bext(1_728_000_000 * 2**4)) + chunk(b"iXML", IXML)
    body += chunk(b"data", b"\0" * 12)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    meta = read_bwf(path)
    assert meta.time_reference == 1_728_000_000 * 2**4  # needs the high word
    assert (meta.originator, meta.originator_reference) == ("Sound Devices", "888")
    assert meta.timecode_rate == Fraction(24000, 1001) and meta.timecode_drop_frame is False
    assert (meta.project, meta.scene, meta.take) == ("Smith Wedding", "Ceremony", "3")
    assert meta.track_names == ("Groom lav", "Officiant")


def test_rf64_long_recording_with_ixml_after_the_data(tmp_path):
    path = tmp_path / "long.wav"
    data = b"\0" * 24
    ds64 = struct.pack("<QQQI", 0, len(data), 4, 0)
    body = b"WAVE" + chunk(b"ds64", ds64) + chunk(b"fmt ", FMT)
    body += b"data" + struct.pack("<I", 0xFFFFFFFF) + data + chunk(b"iXML", IXML)
    path.write_bytes(b"RF64" + struct.pack("<I", 0xFFFFFFFF) + body)
    meta = read_bwf(path)
    assert meta.timecode_rate == Fraction(24000, 1001)
    assert meta.time_reference == 999  # from iXML, since there is no bext chunk


def test_not_a_wav(tmp_path):
    (tmp_path / "x.wav").write_bytes(b"not a riff file at all")
    assert read_bwf(tmp_path / "x.wav") is None
    assert read_bwf(tmp_path / "missing.wav") is None


# ---------------------------------------------------------------------------
# Devices, chapters, rec-run timecode
# ---------------------------------------------------------------------------


def info(path, *, duration=60.0, audio_only=False, make=None, model=None, serial=None, created=None, tc=None, size=1):
    streams = () if audio_only else (video_info(),)
    return MediaInfo(
        path=path, size_bytes=size, mtime_ns=0, container="mp4", duration_s=duration, video=streams,
        audio=(audio_info(),), make=make, model=model, serial=serial, creation_time=created,
        timecode=None if tc is None else TimecodeInfo(seconds=tc, rate=Fraction(25), drop_frame=False, source="tmcd"),
    )  # fmt: skip


def video_info():
    from mcsync.media.probe import VideoStreamInfo

    return VideoStreamInfo(0, "h264", 64, 36, Fraction(25), Fraction(25), False, 0.0, 60.0)


def audio_info():
    from mcsync.media.probe import AudioStreamInfo

    return AudioStreamInfo(1, "aac", 48000, 2, "stereo", 0.0, 60.0)


def test_file_families_and_card_roots():
    assert file_family("GH010042.MP4") == "GoPro"
    assert file_family("C0003.MP4") == "Sony"
    assert file_family("DJI_20260614101500_0001_D.MP4") == "DJI"
    assert file_family("230614_001.WAV") == "Zoom"
    assert file_family("holiday.mp4") is None
    assert card_root("/v/CardA/PRIVATE/M4ROOT/CLIP/C0001.MP4") == Path("/v/CardA").resolve()
    assert card_root("/v/CardB/DCIM/100CANON/MVI_0001.MP4") == Path("/v/CardB").resolve()
    assert card_root("/v/recorder/ZOOM0001.WAV") == Path("/v/recorder").resolve()


def test_identify_device():
    serial = identify_device(info("/v/A/C0001.MP4", make="Sony", model="ILCE-7M4", serial="4001234"))
    assert serial.key.startswith("serial:") and serial.kind == "camera" and serial.name.endswith("#1234")
    same_model = identify_device(info("/v/A/DCIM/100CANON/MVI_0001.MP4", make="Canon", model="EOS R6"))
    other_card = identify_device(info("/v/B/DCIM/100CANON/MVI_0001.MP4", make="Canon", model="EOS R6"))
    assert same_model.key != other_card.key  # two identical bodies, two cards
    recorder = identify_device(info("/v/audio/ZOOM0001.WAV", audio_only=True))
    assert recorder.kind == "recorder"
    phone = identify_device(info("/v/p/IMG_1234.MOV", make="Apple", model="iPhone 16"))
    assert phone.kind == "phone"
    drone = identify_device(info("/v/d/DJI_0001.MP4", make="DJI", model="Mavic 3"))
    assert drone.kind == "drone"
    cams = {identify_device(info(f"/v/cam/C000{k}.MP4")).key for k in range(3)}
    assert len(cams) == 1  # same folder and family: one device


def test_gopro_chapters_old_and_new_naming():
    infos = [
        info("/g/DCIM/100GOPRO/GH020042.MP4"),
        info("/g/DCIM/100GOPRO/GH010042.MP4"),
        info("/g/DCIM/100GOPRO/GH010043.MP4"),
        info("/g/DCIM/100GOPRO/GOPR0007.MP4"),
        info("/g/DCIM/100GOPRO/GP010007.MP4"),
        info("/g/DCIM/100GOPRO/GX030042.MP4"),  # different encoding prefix: another take
    ]
    groups = sorted(find_chapters(infos, ["gopro"] * len(infos)))
    assert groups == [[1, 0], [3, 4]]


def test_four_gigabyte_splits():
    t0 = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)
    from datetime import timedelta

    big = 4 * 2**30 - 10_000
    infos = [
        info("/c/MVI_0001.MOV", duration=1100.0, created=t0, size=big),
        info("/c/MVI_0002.MOV", duration=300.0, created=t0 + timedelta(seconds=1101), size=big // 3),
        info("/c/MVI_0003.MOV", duration=60.0, created=t0 + timedelta(seconds=3000), size=10_000),
    ]
    assert find_chapters(infos, ["canon"] * 3) == [[0, 1]]


def test_rec_run_timecode_detection():
    # Free-run: timecode gaps follow the pauses between takes.
    free = [info("/a/1.MP4", duration=60, tc=36000.0), info("/a/2.MP4", duration=60, tc=36300.0)]
    assert not is_record_run(free)
    # Rec-run: take 2 continues on the frame after take 1, whatever the pause.
    rec = [info("/a/1.MP4", duration=60, tc=3600.0), info("/a/2.MP4", duration=30, tc=3660.0),
           info("/a/3.MP4", duration=10, tc=3690.04)]  # fmt: skip
    assert is_record_run(rec)
    # Chapters of one take are contiguous by nature and prove nothing.
    assert not is_record_run(rec[:2], chapters=[[0, 1]])
    assert not is_record_run([info("/a/1.MP4", tc=3600.0)])


# ---------------------------------------------------------------------------
# Waveform peaks, fingerprints, cache
# ---------------------------------------------------------------------------


def test_peak_pyramid_is_streaming_invariant(tmp_path):
    x = (np.random.default_rng(0).standard_normal(100_003) * 0.2).astype(np.float32)
    whole, pieces = PeakBuilder(), PeakBuilder()
    whole.feed(x)
    for chunk_ in np.array_split(x, 7):
        pieces.feed(chunk_)
    a, b = whole.finish(), pieces.finish()
    assert set(a) == set(PEAK_LEVELS)
    for spb in PEAK_LEVELS:
        np.testing.assert_array_equal(a[spb], b[spb])
        assert len(a[spb]) == -(-len(x) // spb)
    base = decode_peaks(a[64])
    assert base[0, 0] == pytest.approx(x[:64].min(), abs=0.02)
    assert base[0, 1] == pytest.approx(x[:64].max(), abs=0.02)
    write_peaks(tmp_path, a)
    np.testing.assert_array_equal(read_peaks(tmp_path, 1024, 10, 20), a[1024][10:20])
    with pytest.raises(ValueError):
        read_peaks(tmp_path, 100)


def test_quiet_audio_stays_visible_in_peaks():
    builder = PeakBuilder()
    builder.feed(np.full(64, 0.01, dtype=np.float32))  # -40 dBFS
    assert builder.finish()[64][0, 1] >= 20  # μ-law keeps it well above 1 LSB


def test_fingerprint_survives_copies_and_detects_edits(tmp_path):
    data = np.random.default_rng(1).bytes(3 * 2**20 + 123)
    a = tmp_path / "a.mp4"
    a.write_bytes(data)
    b = tmp_path / "renamed.mp4"
    b.write_bytes(data)
    os.utime(b, (1, 1))
    assert fingerprint(a) == fingerprint(b)
    c = tmp_path / "edited.mp4"
    c.write_bytes(data[:-1] + b"x")
    assert fingerprint(c) != fingerprint(a)
    small = tmp_path / "small.wav"
    small.write_bytes(b"abc")
    assert len(fingerprint(small)) == 40


def test_cache_keys_and_lru_eviction(tmp_path):
    cache = AnalysisCache(tmp_path, max_bytes=2500)
    p = SyncParams()
    e1 = cache.entry("aa" * 20, 1, params=p)
    assert e1.directory.parent.name == "aa" * 20
    assert cache.entry("aa" * 20, 1, channel=0, params=p).directory != e1.directory
    assert params_key(SyncParams(band_low_hz=100.0)) != params_key(p)
    entries = []
    for k in range(3):
        e = cache.entry(f"{k:02d}" * 20, 1, params=p)
        e.directory.mkdir(parents=True)
        (e.directory / "pcm.f32").write_bytes(b"\0" * 1000)
        (e.directory / "meta.json").write_text('{"rate": 8000, "samples": 250, "level_dbfs": -20.0}')
        e.touch()
        os.utime(e.directory / "last_used", (1000 + k, 1000 + k))
        entries.append(e)
    assert cache.size_bytes() > 3000
    freed = cache.evict(keep={entries[0].directory})
    assert freed > 0
    assert entries[0].exists()  # oldest, but in use
    assert not entries[1].exists()  # oldest unused
    assert entries[2].exists()
    signal = entries[2].load()
    assert signal.rate == 8000 and len(signal.samples) == 250 and signal.level_dbfs == -20.0
    cache.clear()
    assert cache.entries() == []
