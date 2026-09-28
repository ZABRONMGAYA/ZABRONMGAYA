"""What is footage and what is not: stray files are left out quietly, unreadable media is an error, and likely
copies are only suggestions."""

from __future__ import annotations

import importlib
import json
import subprocess

import pytest

from mcsync.media.probe import ProbeError, is_media_name, parse_probe, probe
from mcsync.media.tools import find_tools
from mcsync.project.db import duplicate_ignored, same_content
from test_project import item


@pytest.fixture(scope="module")
def tools():
    from mcsync.testing.media import ffmpeg_available

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not installed")
    return find_tools()


def test_editing_caches_proxies_and_documents_are_not_probed():
    for name in ("clip.pek", "clip.cfa", "LUT.cube", "offload.mhl", "DJI_0001.LRF", "GX010001.LRV", "notes.docx",
                 "._C0001.MP4"):  # fmt: skip
        assert not is_media_name(name), name
    for name in ("C0001.MP4", "A001.MOD", "M2U00001.MPG", "ZOOM0001.WAV", "clip.360", "TAKE.MXF"):
        assert is_media_name(name), name


def test_unreadable_files_are_told_apart(tools, tmp_path):
    stray = tmp_path / "stray.abc"
    stray.write_bytes(b"not media at all" * 100)
    with pytest.raises(ProbeError) as e:
        probe(stray, tools)
    assert e.value.skip and e.value.reason == "Not a media file"

    broken = tmp_path / "C0001.MP4"
    broken.write_bytes(b"\0\0\0\x18ftypmp42" + b"x" * 2000)
    with pytest.raises(ProbeError) as e:
        probe(broken, tools)
    assert not e.value.skip and e.value.reason.startswith("Damaged or incomplete file")

    raw = tmp_path / "A001_C001.R3D"
    raw.write_bytes(b"RED2" + b"\0" * 4000)
    with pytest.raises(ProbeError) as e:
        probe(raw, tools)
    assert e.value.skip and "RED RAW" in e.value.reason


def test_a_video_without_a_stated_frame_rate_is_kept():
    data = {
        "format": {"duration": "10.0", "format_name": "avi"},
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "r_frame_rate": "0/0",
                     "avg_frame_rate": "0/0", "nb_frames": "250", "duration": "10.0"}],
    }  # fmt: skip
    info = parse_probe(data, path="/m/screen.avi")
    assert float(info.video[0].frame_rate) == 25 and info.video[0].is_vfr


def test_a_file_without_a_stated_length_is_measured(tools, tmp_path, monkeypatch):
    f = tmp_path / "rec.mkv"
    subprocess.run([tools.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:a", "pcm_s16le", str(f)], check=True)  # fmt: skip
    real_run = subprocess.run

    def without_durations(cmd, *args, **kwargs):  # the stream reports no length
        proc = real_run(cmd, *args, **kwargs)
        if "-show_format" in cmd:
            data = json.loads(proc.stdout)
            data["format"].pop("duration", None)
            for s in data["streams"]:
                s.pop("duration", None)
            proc = subprocess.CompletedProcess(proc.args, proc.returncode, json.dumps(data).encode(), proc.stderr)
        return proc

    monkeypatch.setattr(importlib.import_module("mcsync.media.probe").subprocess, "run", without_durations)
    info = probe(f, tools)
    assert info.duration_s == pytest.approx(3.0, abs=0.05)


def test_likely_copies_stay_in_until_the_user_decides():
    assert duplicate_ignored(1, None, "identical")
    assert not duplicate_ignored(1, None, "probable")  # cameras started together look alike
    assert duplicate_ignored(1, "ignore", "probable")
    assert not duplicate_ignored(1, "keep", "identical")
    assert not duplicate_ignored(None, None, None)


def test_same_name_time_and_length_is_only_a_suggestion(project_factory):
    project, tmp_path = project_factory
    a = item(tmp_path / "camA" / "GX010001.MP4", "gopro1", created="2026-06-14T14:00:00+00:00")
    b = item(tmp_path / "camB" / "GX010001.MP4", "gopro2", created="2026-06-14T14:00:00+00:00")
    ida, idb = project.add_media([a, b])
    dups = project.duplicates()
    assert [d["clip_id"] for d in dups] == [idb] and dups[0]["reason"] == "probable"
    assert not project.clip(idb).ignored_duplicate  # synchronised like any other clip
    project.decide_duplicates([dups[0]["media_id"]], "ignore")
    assert project.clip(idb).ignored_duplicate


def test_files_that_share_a_fingerprint_but_differ_inside_are_not_copies(project_factory):
    project, tmp_path = project_factory
    size = 4 << 20
    fa, fb = tmp_path / "ZOOM0001_Tr1.WAV", tmp_path / "ZOOM0001_Tr2.WAV"
    fa.write_bytes(b"\0" * size)
    fb.write_bytes(b"\0" * (1 << 20) + b"\x01" * (size - (2 << 20)) + b"\0" * (1 << 20))  # another take inside
    assert not same_content(str(fa), str(fb))
    a, b = item(fa, "zoom"), item(fb, "zoom")
    b = type(b)(**{**b.__dict__, "fingerprint": a.fingerprint})  # same size, start and end
    project.add_media([a, b])
    assert project.duplicates() == []


@pytest.fixture
def project_factory(tmp_path):
    from mcsync.project import Project

    with Project.create(tmp_path / "p.syncora", "p") as p:
        yield p, tmp_path


def _decoder_that_fails_after(samples: int) -> list[str]:
    """A stand-in for FFmpeg: writes ``samples`` float32 samples, then exits with an error."""
    import sys

    code = f"import sys; sys.stdout.buffer.write(bytes({4 * samples})); sys.stderr.write('Invalid data'); sys.exit(1)"
    return [sys.executable, "-c", code]


def test_audio_decoded_before_a_damaged_end_is_kept(tmp_path):
    from mcsync.media.extract import ExtractionError, _decode_filtered
    from mcsync.sync.params import SyncParams

    params = SyncParams()
    n, _, _ = _decode_filtered(_decoder_that_fails_after(params.analysis_rate * 5), tmp_path / "a.f32", params, 0,
                               None, None)  # fmt: skip
    assert n == params.analysis_rate * 5
    with pytest.raises(ExtractionError, match="Invalid data"):
        _decode_filtered(_decoder_that_fails_after(10), tmp_path / "b.f32", params, 0, None, None)
