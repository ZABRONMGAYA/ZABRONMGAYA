"""Project schema v2: migration from v1 files, the task queue, duplicates, relinking, sessions and transcripts."""

from __future__ import annotations

import json
import os
import sqlite3
from importlib import resources

import pytest

from mcsync.project import Project, ProjectError
from mcsync.project.db import SCHEMA_VERSION
from mcsync.serialize import media_info_to_dict
from test_project import item, media, sample_match


def make_v1_project(path, files):
    """A project file as release 0.1 wrote it: schema v1, raw ffprobe output inside info_json."""
    conn = sqlite3.connect(path)
    schema = resources.files("mcsync.project").joinpath("schema.sql").read_text()
    conn.executescript(f"BEGIN;\n{schema}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO project (id, name, created_at, engine_version) VALUES (1, 'old', '2026-01-01T00:00:00', '0.1.0')"
    )
    conn.execute("INSERT INTO device (key, name, kind) VALUES ('camA', 'CAMA', 'camera')")
    for n, f in enumerate(files, 1):
        info = media_info_to_dict(media(f), include_raw=True)
        conn.execute(
            "INSERT INTO media_file (path, size_bytes, mtime_ns, fingerprint, info_json, added_at) "
            "VALUES (?, 1000, 0, ?, ?, '2026-01-01T00:00:00')",
            (str(f), f"fp{n}", json.dumps(info)),
        )
        conn.execute("INSERT INTO clip (media_id, device_id, name, audio_stream) VALUES (?, 1, ?, 1)", (n, f.name))
    conn.commit()
    conn.close()


def test_opens_and_upgrades_a_v1_project(tmp_path):
    path = tmp_path / "old.mcsync"
    make_v1_project(path, [tmp_path / "A001.MOV", tmp_path / "r.wav"])
    with Project.open(path) as p:
        assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        rows = p.media_rows()
        assert [r["name"] for r in rows] == ["A001.MOV", "r.wav"]
        assert rows[0]["kind"] == "video" and rows[0]["fps"] == "24000/1001" and rows[0]["duration_s"] == 60.0
        assert rows[0]["width"] == 1920 and rows[0]["sample_rate"] == 48000
        # raw ffprobe output moved out of the metadata every list reads
        conn = sqlite3.connect(path)
        assert "raw" not in json.loads(conn.execute("SELECT info_json FROM media_file").fetchone()[0])
        assert conn.execute("SELECT COUNT(*) FROM media_probe").fetchone()[0] == 2
        # everything else carries on
        assert [c.name for c in p.clips()] == ["A001.MOV", "r.wav"]
        assert p.clips()[0].info.video[0].width == 1920
    with Project.open(path) as p:  # opening again is a no-op
        assert len(p.clips()) == 2


def test_rejects_a_newer_project(tmp_path):
    path = tmp_path / "future.syncora"
    Project.create(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(ProjectError, match="schema"):
        Project.open(path)


def test_new_project_indexes_metadata(tmp_path):
    with Project.create(tmp_path / "p.syncora") as p:
        p.add_media([item(tmp_path / "A001.MOV"), item(tmp_path / "r.wav", "rec", audio_only=True)])
        rows = {r["name"]: r for r in p.media_rows()}
        assert rows["A001.MOV"]["kind"] == "video" and rows["r.wav"]["kind"] == "audio"
        assert rows["r.wav"]["fps"] is None and rows["r.wav"]["channels"] == 2
        stats = p.database_stats()
        assert stats["rows"]["media_file"] == 2 and stats["bytes"] > 0


# ------------------------------------------------------------------ task queue


@pytest.fixture
def project(tmp_path):
    with Project.create(tmp_path / "q.syncora") as p:
        yield p


def test_queue_claims_by_priority_and_finishes(project):
    assert project.enqueue("analyze", [{"target": i, "clip_id": i} for i in range(1, 6)]) == 5
    project.enqueue("analyze", [{"target": 4, "clip_id": 4, "priority": 1}])  # more urgent: moves up
    project.enqueue("probe", [{"target": "d1"}])
    first = project.claim(["analyze"], 2)
    assert [t.target for t in first] == ["4", "1"] and all(t.attempts == 1 for t in first)
    assert project.task_counts()["analyze"]["running"] == 2
    project.finish_tasks([(first[0].id, "done", None), (first[1].id, "failed", "bad file")])
    counts = project.task_counts()
    assert counts["analyze"] == {"pending": 3, "running": 0, "done": 1, "failed": 1, "skipped": 0, "cancelled": 0}
    assert counts["probe"]["pending"] == 1
    assert project.pending_count(["analyze"]) == 3
    errors = project.tasks(statuses=["failed"])
    assert errors[0]["error"] == "bad file" and errors[0]["target"] == "1"


def test_queue_keeps_finished_work_unless_requeued(project):
    project.enqueue("analyze", [{"target": 1}])
    (task,) = project.claim(["analyze"], 5)
    project.finish_tasks([(task.id, "done", None)])
    assert project.enqueue("analyze", [{"target": 1}]) == 1  # the row is touched but stays done
    assert project.task_counts()["analyze"]["done"] == 1
    project.enqueue("analyze", [{"target": 1}], requeue=True)
    assert project.task_counts()["analyze"]["pending"] == 1


def test_running_tasks_return_to_the_queue_after_a_crash(tmp_path):
    path = tmp_path / "crash.syncora"
    p = Project.create(path)
    p.enqueue("analyze", [{"target": i} for i in range(3)])
    p.claim(["analyze"], 2)
    p._conn.close()  # the process dies: nothing finishes the running tasks
    with Project.open(path) as again:
        assert again.task_counts()["analyze"]["pending"] == 3
        assert [t.attempts for t in again.claim(["analyze"], 3)] == [2, 2, 1]


def test_cancel_retry_and_prioritize(project):
    project.enqueue("analyze", [{"target": i, "clip_id": i} for i in range(1, 7)])
    assert project.cancel_tasks(clip_ids=[1, 2]) == 2
    assert project.prioritize([6], 0) == 1
    assert project.claim(["analyze"], 1)[0].clip_id == 6
    (t,) = project.claim(["analyze"], 1)
    project.finish_tasks([(t.id, "failed", "boom")])
    assert project.cancel_tasks() == 2  # everything still pending
    counts = project.task_counts()["analyze"]
    assert counts["cancelled"] == 4 and counts["failed"] == 1 and counts["running"] == 1
    assert project.retry_tasks() == 1  # failed ones
    assert project.retry_tasks(statuses=["cancelled"], clip_ids=[1]) == 1
    assert project.task_counts()["analyze"]["pending"] == 2
    assert project.purge_tasks(["analyze"], ["cancelled"]) == 3


def test_removing_clips_removes_their_tasks_but_never_files(project, tmp_path):
    f = tmp_path / "A001.MOV"
    f.write_bytes(b"x")
    (clip_id,) = project.add_media([item(f)])
    project.enqueue("analyze", [{"target": clip_id, "clip_id": clip_id}])
    project.remove_clips([clip_id])
    assert project.clips() == [] and project.task_counts() == {}
    assert f.exists()


# ------------------------------------------------------------------ duplicates


def test_duplicates_are_recorded_never_deleted(project, tmp_path):
    a, b = item(tmp_path / "card1" / "C0001.MP4"), item(tmp_path / "backup" / "C0001.MP4")
    b = type(b)(**{**b.__dict__, "fingerprint": a.fingerprint})  # same content, another place
    ida, idb = project.add_media([a, b])
    dups = project.duplicates()
    assert len(dups) == 1 and dups[0]["clip_id"] == idb and dups[0]["decision"] is None
    clips = {c.id: c for c in project.clips()}
    assert clips[idb].ignored_duplicate and not clips[ida].ignored_duplicate
    project.decide_duplicates([dups[0]["media_id"]], "keep")
    assert not project.clip(idb).ignored_duplicate
    with pytest.raises(ProjectError):
        project.decide_duplicates([dups[0]["media_id"]], "delete")


# --------------------------------------------------------- offline media, relink


def test_offline_media_and_relink(tmp_path):
    drive = tmp_path / "E"
    drive.mkdir()
    f = drive / "A001.MOV"
    f.write_bytes(b"12345")
    st = f.stat()
    path = tmp_path / "p.syncora"
    with Project.create(path) as p:
        it = item(f)
        it = type(it)(**{**it.__dict__, "info": type(it.info)(**{**it.info.__dict__, "size_bytes": st.st_size,
                                                                   "mtime_ns": st.st_mtime_ns})})  # fmt: skip
        p.add_media([it])
    moved = tmp_path / "F" / "A001.MOV"
    moved.parent.mkdir()
    os.replace(f, moved)
    with Project.open(path) as p:
        (off,) = p.offline_media()
        assert off["status"] == "offline" and off["filename"] == "A001.MOV"
        st = moved.stat()
        p.relink([(off["media_id"], str(moved), st.st_size, st.st_mtime_ns)])
        assert p.offline_media() == []
        assert p.clips()[0].path == str(moved) and p.clips()[0].info.path == str(moved)
    with Project.open(path) as p:
        assert p.refresh_media_status() == {"online": 1}


# ------------------------------------------------------------ discovery, sessions


def test_discovery_is_incremental(project, tmp_path):
    root = project.add_import_root(str(tmp_path))
    assert project.add_import_root(str(tmp_path)) == root
    new = project.add_discovered([(str(tmp_path / f"{i}.MOV"), 10, 0, "video", root) for i in range(3)])
    assert len(new) == 3
    again = project.add_discovered([(str(tmp_path / f"{i}.MOV"), 10, 0, "video", root) for i in range(4)])
    assert len(again) == 1  # only the new file
    project.finish_discovered([(new[0], "done", None, None), (new[1], "failed", None, "not media")])
    counts = project.discovery_counts()
    assert counts == {"total": 4, "by_kind": {"video": 4}, "by_status": {"pending": 2, "done": 1, "failed": 1}}
    assert project.discovered([new[1]])[new[1]]["error"] == "not media"


def test_sessions_keep_manual_assignments(project, tmp_path):
    ids = project.add_media([item(tmp_path / f"A00{i}.MOV") for i in range(4)])
    project.replace_auto_sessions([{"label": "Session 1", "group_no": 0, "clip_ids": ids[:2]},
                                   {"label": "Session 2", "group_no": 1, "clip_ids": ids[2:]}])  # fmt: skip
    manual = project.create_session("Ceremony", [ids[0]])
    project.replace_auto_sessions([{"label": "Session 1", "group_no": 0, "clip_ids": ids}])
    by_label = {s["label"]: s for s in project.sessions()}
    assert by_label["Ceremony"]["clips"] == 1 and by_label["Session 1"]["clips"] == 3
    assert project.clip(ids[0]).session_id == manual
    project.assign_session([ids[0]], None)  # the manual session empties and goes away
    assert "Ceremony" not in {s["label"] for s in project.sessions()}


# --------------------------------------------------------- transcripts, markers


def test_transcripts_markers_and_search(project, tmp_path):
    (clip_id,) = project.add_media([item(tmp_path / "A001.MOV")])
    project.add_transcript_segments([
        (clip_id, 1.0, 3.0, "S1", "en", "Welcome everyone to the ceremony", 0.9),
        (clip_id, 3.0, 5.0, "S2", "en", "Please take your seats", 0.8),
    ])  # fmt: skip
    assert [s["text"] for s in project.transcript(clip_id)][0].startswith("Welcome")
    assert [s["start_s"] for s in project.search_transcripts("ceremony")] == [1.0]
    assert project.search_transcripts("nothing like this") == []
    project.add_markers([(clip_id, 2.0, "applause", None, 0.7), (clip_id, 9.0, "speech", "vows", None)])
    assert [m["t_s"] for m in project.markers(marker_type="applause")] == [2.0]
    assert len(project.markers(clip_id)) == 2
    project.remove_clips([clip_id])
    assert project.search_transcripts("ceremony") == []


def test_matches_batch_and_reuse(project, tmp_path):
    ids = project.add_media([item(tmp_path / "a.MOV"), item(tmp_path / "b.MOV", "camB")])
    run = project.start_run({})
    m = sample_match(str(ids[0]), str(ids[1]))
    project.save_matches(run, [("k1", m, "fingerprint")])
    assert project.known_pair_keys(["k1", "k2"]) == {"k1"}
    (summary,) = project.run_match_summaries(run)
    assert summary["method"] == "fingerprint" and summary["offset_s"] == pytest.approx(m.offset_s)
    run2 = project.start_run({})
    assert project.runs()[0]["status"] == "failed"  # an unfinished run is closed when the next starts
    assert project.copy_matches(run2, [("k1", ids[1], ids[0]), ("missing", 1, 2)]) == 1
    (copied,) = project.run_matches(run2)
    assert (copied.ref_id, copied.tgt_id) == (str(ids[1]), str(ids[0]))
