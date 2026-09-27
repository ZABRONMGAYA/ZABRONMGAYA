"""The production pipeline on a small multi-session production, through the staged (large-project) path."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcsync.pipeline import runner
from mcsync.service.app import EngineService
from mcsync.testing.media import ffmpeg_available
from mcsync.testing.production import ProductionPlan, evaluate, generate_production

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="FFmpeg is not installed")

SMALL = ProductionPlan(sessions=2, cameras=3, clips_per_camera=4, recorders=1, files_per_recorder=2,
                       recorder_file_s=90.0, clip_s=(12.0, 25.0), problems=True, seed=3)  # fmt: skip


@pytest.fixture(scope="module")
def production(tmp_path_factory):
    return generate_production(tmp_path_factory.mktemp("production") / "SHOOT", SMALL, workers=4)


@pytest.fixture
def service(tmp_path):
    notes: list[tuple[str, dict]] = []
    svc = EngineService(notify=lambda m, p: notes.append((m, p)), cache_dir=str(tmp_path / "cache"), workers=2)
    svc.notes = notes  # type: ignore[attr-defined]
    yield svc
    svc.close()


def run_job(svc: EngineService, started: dict) -> dict:
    job = svc.jobs.wait(started["job_id"], timeout=600)
    assert job.status == "done", job.error
    return job.result


def test_staged_pipeline_synchronises_a_multi_session_production(service, production, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "EXHAUSTIVE_PAIR_BUDGET", 0)  # force the large-production path
    service.project_create(str(tmp_path / "fest.syncora"), "Festival")
    imported = run_job(service, service.media_import([str(production.root)]))
    problems = {Path(p["path"]).name for p in imported["problems"]}
    assert {f"D000{k}.MP4" for k in range(1, 6)} <= problems  # damaged files are reported, the rest continues
    assert len(service.project.duplicates()) == 5

    result = run_job(service, service.sync_run())
    assert result["strategy"] == "staged"
    score = evaluate(production, service.media_index())
    assert score["sync_files"] == {"exact": SMALL.video_files + SMALL.audio_files}, score
    assert score["sessions_one_group"] == SMALL.sessions
    by = score["by_condition"]
    assert set(by["damaged"]) == {"failed"} and set(by["duplicate"]) == {"skipped"}
    for problem in ("silent", "no-audio", "unrelated"):
        assert "placed by audio (wrong)" not in by[problem], by[problem]

    summary = service.sync_summary()
    counts = summary["counts"]
    assert counts["failed"] == 5 and summary["unreadable_files"] == 5  # the damaged files
    assert counts["skipped"] == 5  # one of each pair of identical copies
    # Muted, drone and unrelated clips: only their own camera's clock places them, so they need manual sync.
    assert counts["manual"] + counts["review"] >= 25 and counts["manual"] >= 20
    assert counts["synchronized"] >= SMALL.video_files + SMALL.audio_files - 2  # a backup folder mixes two cameras
    assert sum(counts[c] for c in counts if c != "synchronized") == summary["clips"] + 5
    assert len(summary["sessions"]) == SMALL.sessions

    # Nothing changed: the next run reuses every verified pair.
    again = run_job(service, service.sync_run())
    assert again["matched"] == 0 and again["reused"] == result["pairs"]


def test_pause_resume_cancel_retry_and_restart(service, production, tmp_path):
    service.project_create(str(tmp_path / "control.syncora"))
    pipeline = service.pipeline
    pipeline.pause()
    service.media_add([str(production.root)])
    deadline = time.time() + 60
    while pipeline.import_request(1).walking and time.time() < deadline:
        time.sleep(0.05)
    counts = service.project.task_counts()["probe"]
    assert counts["pending"] > 0 and counts["done"] == 0  # found, but nothing starts while paused
    service.pipeline_resume()
    deadline = time.time() + 300
    while service.project.pending_count(["probe", "analyze"]) and time.time() < deadline:
        time.sleep(0.1)
    counts = service.project.task_counts()
    assert counts["probe"]["failed"] == 5  # the damaged files
    errors = service.tasks_list(statuses=["failed"])
    assert {Path(e["name"]).name for e in errors} >= {"D0001.MP4"}
    assert service.tasks_retry(kinds=["probe"])["retried"] == 5
    deadline = time.time() + 60
    while service.project.pending_count(["probe"]) and time.time() < deadline:
        time.sleep(0.05)
    assert service.project.task_counts()["probe"]["failed"] == 5  # still damaged: never fatal

    # Re-analysing a clip and cancelling it.
    clip = next(r.id for r in service.project.clips() if r.audio_stream is not None)
    service.pipeline_pause()
    assert service.tasks_analyze([clip])["queued"] == 1
    assert service.tasks_cancel(clip_ids=[clip])["cancelled"] >= 1
    assert service.project.task_counts()["analyze"]["cancelled"] == 1

    # A sync run interrupted by closing the project is offered for resuming on open.
    service.sync_start()
    service.project_close()
    info = service.project_open(str(tmp_path / "control.syncora"))
    assert info["resume"] is not None and info["resume"]["sync"]["phase"] in ("waiting", "planning", "matching")
    assert info["paused"] is True
    service.pipeline_restart(sync=True)
    deadline = time.time() + 600
    while (service.pipeline.sync_state or {}).get("phase") != "done" and time.time() < deadline:
        time.sleep(0.2)
    assert service.pipeline.sync_state["phase"] == "done"
    assert service.project_info()["resume"] is None
