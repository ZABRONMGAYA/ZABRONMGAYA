"""Transcription, speakers, markers and search (mcsync.ai), from the building blocks to the engine methods."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from mcsync.ai import speakers as voices
from mcsync.ai.models import ModelStore, bundled_dir
from mcsync.ai.speech import CHUNK_S, caption_event, chunks
from mcsync.project import Project
from mcsync.service.ai_methods import smart_targets
from mcsync.service.app import EngineService
from mcsync.sync.types import ClipPlacement, PlacementMethod, PlacementStatus
from mcsync.testing.media import ffmpeg_available
from test_project import item

DIALOG = Path(__file__).parent / "data" / "dialog.ogg"  # two synthetic voices, 22 s (see tests/data/README.md)


def models_installed() -> bool:
    store = ModelStore(Path("/nonexistent"))
    return bundled_dir() is not None and all(
        store.path(m) is not None for m in ("whisper-base", "silero-vad", "voice-eres2net")
    )


needs_models = pytest.mark.skipif(not (models_installed() and ffmpeg_available()),
                                  reason="the bundled AI models or FFmpeg are not installed")  # fmt: skip


# ------------------------------------------------------------------ building blocks


def test_recordings_are_transcribed_in_chunks():
    assert chunks(30.0) == [(0.0, 30.0)]
    parts = chunks(2.5 * CHUNK_S)
    assert [round(a) for a, _ in parts] == [0, 600, 1200] and parts[-1][1] == 2.5 * CHUNK_S


def test_captions_become_sound_events_not_text():
    assert caption_event("(applause)") == "Applause"
    assert caption_event("[Music]") == "Music"
    assert caption_event("♪ ♪") == "Music"
    assert caption_event("(laughs)") == "Laughter"
    assert caption_event("Thank you all for coming.") is None


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_utterances_are_grouped_by_voice_across_recordings():
    rng = np.random.default_rng(1)
    a, b = unit(rng.normal(size=64)), unit(rng.normal(size=64))  # two voices: nearly orthogonal
    noisy = lambda v: unit(v + 0.3 * rng.normal(size=64) / 8)  # noqa: E731
    known: list[voices.Speaker] = []
    keys = voices.assign([noisy(a), noisy(b), None, noisy(a), noisy(b)], known)
    assert keys[0] == keys[3] and keys[1] == keys[4] and keys[0] != keys[1] and keys[2] is None
    assert [s.count for s in known] == [2, 2]
    # A voice first heard as a new speaker is merged once its fingerprints show it is the same.
    known.append(voices.Speaker("S09", noisy(a) * 1, 1))
    assert voices.merges(known) == [("S09", keys[0])]


def test_smart_scope_transcribes_each_moment_once(tmp_path):
    with Project.create(tmp_path / "p.syncora", "p") as project:
        rec, cam_a, cam_b, lone = project.add_media([
            item(tmp_path / "ZOOM0001.WAV", "zoom", audio_only=True), item(tmp_path / "A001.MOV", "camA"),
            item(tmp_path / "B001.MOV", "camB"), item(tmp_path / "C001.MOV", "camC"),
        ])  # fmt: skip
        rows = project.clips()
        placed = lambda cid, start: ClipPlacement(str(cid), start, 0, PlacementMethod.AUDIO, 1.0,  # noqa: E731
                                                  PlacementStatus.SYNCED)  # fmt: skip
        placements = {rec: placed(rec, 0.0), cam_a: placed(cam_a, 5.0), cam_b: placed(cam_b, 100.0)}
        # The recorder (0–60 s) covers camera A (5–65 s) but for 5 s; camera B (100–160 s) adds time; C is not placed.
        assert sorted(smart_targets(rows, placements, {})) == sorted([rec, cam_b, lone])


def test_transcripts_markers_speakers_and_search_in_the_project(tmp_path):
    with Project.create(tmp_path / "p.syncora", "p") as project:
        (clip,) = project.add_media([item(tmp_path / "ZOOM0001.WAV", "zoom", audio_only=True)])
        fp = unit([1, 0, 0]).tobytes()
        utterances = [(1.0, 4.0, "S01", "en", "Welcome to the wedding of Anna and David", fp),
                      (5.0, 7.0, "S02", "en", "Please raise your glasses", None)]  # fmt: skip
        project.replace_transcript_chunk(clip, 0, (0.0, 600.0), utterances, [(8.0, "Applause")])
        project.replace_transcript_chunk(clip, 0, (0.0, 600.0), utterances, [(8.0, "Applause")])  # a re-run
        assert [s["text"] for s in project.transcript(clip)] == [u[4] for u in utterances]
        assert [(m["t_s"], m["label"], m["source"]) for m in project.markers(clip)] == [(8.0, "Applause", "speech")]
        hits = project.search_transcripts("wedding of anna")
        assert [(h["text"], h["match"]) for h in hits] == [(utterances[0][4], "phrase")]
        assert project.search_transcripts("wedding anna")[0]["match"] == "all words"
        assert project.search_transcripts("anna glasses")[0]["match"] == "some words"
        project.save_speakers([("S01", fp, 1), ("S02", fp, 1)])
        project.rename_speaker("S01", "Father of the bride")
        project.merge_speakers("S02", "S01")
        assert {s["speaker"] for s in project.transcript(clip)} == {"S01"}
        assert [(s["key"], s["name"], s["segments"]) for s in project.speakers()] == [("S01", "Father of the bride", 2)]
        marker = project.add_marker(clip, 3.5, "First kiss")
        project.update_marker(marker, "First kiss!")
        assert [m["label"] for m in project.markers(clip)] == ["First kiss!", "Applause"]


# ------------------------------------------------------------------ with the models


@pytest.fixture
def service(tmp_path):
    svc = EngineService(cache_dir=str(tmp_path / "cache"), workers=1)
    yield svc
    svc.close()


def wait_for_transcripts(svc: EngineService, timeout: float = 240.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = svc.project.task_counts().get("transcribe", {})  # type: ignore[union-attr]
        if c.get("pending", 0) + c.get("running", 0) == 0 and sum(c.values()):
            return
        time.sleep(0.5)
    raise AssertionError(f"transcription did not finish: {svc.project.task_counts()}")  # type: ignore[union-attr]


@needs_models
def test_a_dialog_is_transcribed_with_its_speakers_and_found_by_search(service, tmp_path):
    service.project_create(str(tmp_path / "toast.syncora"), "Toast")
    job = service.jobs.wait(service.media_import([str(DIALOG)])["job_id"], timeout=120)
    assert job.status == "done", job.error
    status = service.ai_status()
    assert status["ready"] and status["speech_engine"], status["problem"]
    assert service.transcripts_start(scope="all")["clips"] == 1
    wait_for_transcripts(service)

    (clip,) = service.project.clips()  # type: ignore[union-attr]
    segments = service.transcript_get(clip.id)["segments"]
    text = " ".join(s["text"] for s in segments).lower()
    assert "father of the bride" in text and "happy" in text, text
    # Times come from the voice detector: the first line starts after 1.5 s of silence.
    assert segments[0]["start_s"] == pytest.approx(1.6, abs=0.3)
    assert {s["language"] for s in segments} == {"en"}
    # Two voices take turns: Peter says lines 1 and 3, the other voice lines 2 and 4.
    who = {k: next(s["speaker"] for s in segments if k in s["text"].lower()) for k in ("father", "happy we", "glasses",
                                                                                      "cheers")}  # fmt: skip
    assert who["father"] == who["glasses"] and who["happy we"] == who["cheers"] != who["father"], who
    speakers = [s["speaker"] for s in segments]
    assert len(set(speakers)) == 2, speakers
    assert service.transcripts_overview()["clips"][0]["status"] == "done"

    service.speakers_rename(who["father"], "Peter")
    hits = service.search_query("father of the bride")["results"]
    assert hits[0]["clip_id"] == clip.id and hits[0]["score"] == 1.0 and hits[0]["speaker_name"] == "Peter"
    assert any(h["matched_by"] == ["speaker"] for h in service.search_query("Peter")["results"])
