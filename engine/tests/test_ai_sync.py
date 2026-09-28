"""AI sync: a clip audio fingerprints could not place is placed from speech, light changes, audio at close range
and the camera clocks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from mcsync.ai import visual
from mcsync.ai.aisync import Candidate, Evidence, Placed, Segment, rank, speech_candidates
from mcsync.media.tools import find_tools
from mcsync.service.app import EngineService
from mcsync.sync.types import ClipPlacement, PlacementMethod, PlacementStatus, SyncResult
from test_ai_speech import DIALOG, needs_models, wait_for_transcripts

needs_ffmpeg = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0, reason="FFmpeg is not installed"
)


def test_sentences_heard_in_two_recordings_give_the_offset():
    recorder = Placed(1, 0, 100.0, 600.0, [
        Segment(10.0, 13.0, "Good afternoon everyone, my name is Peter."),
        Segment(20.0, 24.0, "Please raise your glasses for Anna and David."),
        Segment(30.0, 32.0, "Cheers to the happy couple!"),
    ])  # fmt: skip
    other = Placed(2, 1, 0.0, 600.0, [Segment(5.0, 6.0, "Cheers!")])  # too short to count
    # The camera started 7.5 s into the recorder's clip: its sentences come 7.5 s earlier in its own time.
    camera = [Segment(12.5, 16.6, "please raise your glasses for anna and david"), Segment(22.4, 24.5, "cheers to "
              "the happy couple")]  # fmt: skip
    (best,) = rank(speech_candidates(camera, [recorder, other]))
    assert best.anchor_clip_id == 1 and best.offset_s == pytest.approx(7.5, abs=0.05)
    assert best.start_s == pytest.approx(107.5, abs=0.05)
    assert best.evidence[0].lane == "speech" and "2 sentences" in best.evidence[0].note
    assert 0.5 < best.confidence <= 0.95  # speech alone never reaches the level only audio can confirm


def test_evidence_reinforces_and_the_lanes_show_where():
    c = Candidate(1, 0, 7.5, 7.5, [Evidence("speech", 0.6, "", [(12.5, 0.9), (22.4, 0.7)])])
    alone = rank([c])[0].confidence
    c.evidence.append(Evidence("audio", 0.9, "", [(12.0, 0.9)]))
    assert rank([c])[0].confidence > alone
    lanes = c.lanes(48.0)
    assert lanes["speech"][12] == 3 and lanes["speech"][22] == 2 and sum(lanes["speech"]) == 5


def _flash_video(path: Path, flashes: list[float], duration: float, base: str) -> None:
    enable = "+".join(f"between(t,{t:.2f},{t + 0.12:.2f})" for t in flashes)
    vf = f"drawbox=x=0:y=0:w=64:h=36:color=white:t=fill:enable='{enable}',noise=alls=20:allf=t"
    subprocess.run([find_tools().ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color={base}:s=64x36:r=25:d={duration}", "-vf", vf, "-c:v", "mpeg4", "-q:v", "4", str(path)],
                   check=True)  # fmt: skip


@needs_ffmpeg
def test_light_changes_seen_by_two_cameras_give_their_offset(tmp_path):
    flashes = [3.0, 7.4, 8.1, 13.6, 19.2, 24.0]
    a, b = tmp_path / "A.mp4", tmp_path / "B.mp4"
    _flash_video(a, flashes, 30.0, "0x202020")
    _flash_video(b, [t - 4.3 for t in flashes if t > 4.3], 24.0, "0x505a40")  # B started 4.3 s later, brighter
    lum_a = visual.brightness(find_tools(), str(a), tmp_path / "cache", "aa11")
    lum_b = visual.brightness(find_tools(), str(b), tmp_path / "cache", "bb22")
    assert len(lum_a) == pytest.approx(300, abs=2) and (tmp_path / "cache" / "aa" / "aa11").is_dir()
    found = visual.correlate(lum_b, lum_a, (-20.0, 20.0))  # time in A = time in B + lag
    assert found is not None and found.lag_s == pytest.approx(4.3, abs=0.1)
    assert found.ratio > 1.5 and len(found.shared_events) >= 4


@needs_models
def test_a_camera_with_poor_audio_is_placed_from_its_speech(tmp_path):
    tools = find_tools()
    recorder, camera = tmp_path / "ZOOM0001.WAV", tmp_path / "C0001.MOV"
    subprocess.run([tools.ffmpeg, "-v", "error", "-i", str(DIALOG), "-ar", "48000", str(recorder)], check=True)
    # The camera started 5.3 s later, far from the speakers: thin, noisy sound.
    subprocess.run([tools.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=black:s=64x36:r=25:d=16",
                    "-ss", "5.3", "-i", str(DIALOG), "-f", "lavfi", "-i", "anoisesrc=d=16:c=pink:a=0.03",
                    "-filter_complex", "[1:a]highpass=f=400,lowpass=f=2800,volume=0.5[s];[s][2:a]amix=inputs=2:"
                    "duration=first[a]", "-map", "0:v", "-map", "[a]", "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
                    str(camera)], check=True)  # fmt: skip
    svc = EngineService(cache_dir=str(tmp_path / "cache"), workers=1)
    try:
        svc.project_create(str(tmp_path / "toast.syncora"), "Toast")
        job = svc.jobs.wait(svc.media_import([str(recorder), str(camera)])["job_id"], timeout=120)
        assert job.status == "done", job.error
        rows = {r.name: r for r in svc.project.clips()}  # type: ignore[union-attr]
        rec, cam = rows["ZOOM0001.WAV"], rows["C0001.MOV"]
        # As after a sync where audio fingerprints placed the recorder but not the camera:
        placed = ClipPlacement(str(rec.id), 0.0, 0, PlacementMethod.REFERENCE, 1.0, PlacementStatus.SYNCED)
        svc.project.save_placements(SyncResult(str(rec.id), {str(rec.id): placed}, [], []))  # type: ignore[union-attr]
        svc.transcripts_start(scope="clips", clip_ids=[rec.id])
        wait_for_transcripts(svc)

        result = svc._ai_sync(cam.id)
        best = result["candidates"][0]
        assert best["anchor_clip_id"] == rec.id and best["offset_s"] == pytest.approx(5.3, abs=0.05)
        lanes = {e["lane"]: e for e in best["evidence"]}
        # In the noise the camera's transcript is poor: one sentence is enough, and the audio at close range confirms.
        assert lanes["speech"]["score"] > 0.5 and "in both" in lanes["speech"]["note"]
        assert lanes["audio"]["score"] >= 0.7 and best["confidence"] > 0.95
        assert len(best["lanes"]["speech"]) == 48
        assert svc.ai_sync_result(cam.id)["status"] == "proposed"

        timeline = svc.ai_sync_accept(cam.id)
        clips = {c["clip_id"]: c for g in timeline["groups"] for c in g["clips"]}
        assert clips[cam.id]["start_s"] - clips[rec.id]["start_s"] == pytest.approx(5.3, abs=0.05)
        assert clips[cam.id]["method"] == "manual"  # accepted by the user: a correction, which Undo reverts
    finally:
        svc.close()


def test_the_automatic_fallback_only_proposes_for_review(tmp_path):
    svc = EngineService(cache_dir=str(tmp_path / "cache"), workers=1)
    try:
        from test_project import item

        svc.project_create(str(tmp_path / "p.syncora"), "p")
        project = svc.project
        rec, cam = project.add_media([item(tmp_path / "R.WAV", "zoom", audio_only=True),  # type: ignore[union-attr]
                                      item(tmp_path / "C.MOV", "cam")])  # fmt: skip
        placed = ClipPlacement(str(rec), 0.0, 0, PlacementMethod.REFERENCE, 1.0, PlacementStatus.SYNCED)
        base = SyncResult(str(rec), {str(rec): placed}, [], [])
        proposal = {"source": "auto", "candidates": [{"anchor_clip_id": rec, "offset_s": 12.0, "confidence": 0.7}]}
        project.set_ai_analysis(cam, "sync", proposal, 0.7, "proposed")  # type: ignore[union-attr]
        assert svc.with_ai_proposals(base) is not None and str(cam) not in svc.with_ai_proposals(base).placements
        svc.project_update_settings(ai_fallback=True)
        p = svc.with_ai_proposals(base).placements[str(cam)]
        assert (p.start_s, p.method, p.status) == (12.0, PlacementMethod.AI, PlacementStatus.NEEDS_REVIEW)
        project.set_ai_status(cam, "sync", "rejected")  # type: ignore[union-attr]
        assert str(cam) not in svc.with_ai_proposals(base).placements
    finally:
        svc.close()


def test_brightness_changes_are_normalised():
    lum = np.array([50, 50, 51, 50, 200, 52, 50, 50], dtype=np.float32)
    assert visual.events(lum) == [pytest.approx(0.4)]
