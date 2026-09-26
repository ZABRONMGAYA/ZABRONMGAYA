"""The JSON-RPC engine service, in-process and as a real child process over stdio."""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mcsync.service.app import EngineService
from mcsync.service.rpc import (
    APP_ERROR,
    BUSY,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NO_PROJECT,
    PARSE_ERROR,
    JsonRpcServer,
)

# ---------------------------------------------------------------------------
# In-process
# ---------------------------------------------------------------------------


class InProcess:
    def __init__(self, tmp_path: Path, workers: int = 1) -> None:
        self.out = io.StringIO()
        self.server = JsonRpcServer(io.StringIO(), self.out, log=io.StringIO())
        self.service = EngineService(self.server, cache_dir=str(tmp_path / "cache"), workers=workers)
        self._id = 0

    def raw(self, line: str) -> dict | None:
        return self.server.handle_line(line)

    def call(self, method: str, **params):
        self._id += 1
        response = self.raw(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}))
        assert response is not None and response["id"] == self._id
        if "error" in response:
            raise RpcFailure(response["error"])
        return json.loads(json.dumps(response["result"]))  # exactly what a client would receive

    def run_job(self, method: str, **params) -> dict:
        job = self.service.jobs.wait(self.call(method, **params)["job_id"], timeout=300)
        assert job.status == "done", job.error
        return json.loads(json.dumps(job.result))

    def notifications(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines()]


class RpcFailure(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(error["message"])
        self.code = error["code"]


def by_name(timeline: dict) -> dict[str, dict]:
    return {c["name"]: c for g in timeline["groups"] for c in g["clips"]} | {c["name"]: c for c in timeline["unsynced"]}


@pytest.fixture
def svc(tmp_path):
    s = InProcess(tmp_path)
    yield s
    s.service.close()


def test_protocol_errors(svc):
    assert svc.raw("{nope")["error"]["code"] == PARSE_ERROR
    assert svc.raw('{"id": 1, "method": "engine.hello"}')["error"]["code"] == INVALID_REQUEST
    assert svc.raw("[1, 2]")["error"]["code"] == INVALID_REQUEST
    missing = svc.raw('{"jsonrpc": "2.0", "id": 2, "method": "engine.fly"}')
    assert missing["error"]["code"] == METHOD_NOT_FOUND
    bad = svc.raw('{"jsonrpc": "2.0", "id": 3, "method": "project.open", "params": {"file": "x"}}')
    assert bad["error"]["code"] == INVALID_PARAMS
    positional = svc.raw('{"jsonrpc": "2.0", "id": 4, "method": "project.open", "params": ["x"]}')
    assert positional["error"]["code"] == INVALID_PARAMS
    assert svc.raw('{"jsonrpc": "2.0", "method": "engine.hello"}') is None  # notification: no response
    with pytest.raises(RpcFailure) as err:
        svc.call("media.list")
    assert err.value.code == NO_PROJECT
    with pytest.raises(RpcFailure) as err:
        svc.call("project.open", path="/nowhere/at/all.mcsync")
    assert err.value.code == APP_ERROR
    hello = svc.call("engine.hello", client="tests")
    assert hello["protocol"] == 1 and hello["version"]


def test_busy_and_cancelled_jobs(svc, tmp_path):
    svc.call("project.create", path=str(tmp_path / "p.mcsync"))
    job = svc.service.jobs.start("sync", lambda j: j.cancel.wait(30) and j.check_cancelled())
    with pytest.raises(RpcFailure) as err:
        svc.call("media.import", paths=[str(tmp_path)])
    assert err.value.code == BUSY
    assert svc.call("job.list")[0]["status"] == "running"
    assert svc.call("job.cancel", job_id=job.id) == {"cancelled": True}
    svc.service.jobs.wait(job.id, timeout=10)
    assert job.status == "cancelled"
    failed = [n for n in svc.notifications() if n.get("method") == "job.failed"]
    assert failed and failed[-1]["params"]["cancelled"] is True
    assert svc.call("job.cancel", job_id=job.id) == {"cancelled": False}
    with pytest.raises(RpcFailure):
        svc.call("project.update_settings", colour="blue")


def test_full_workflow(svc, tmp_path, wedding_shoot):
    shoot = wedding_shoot
    info = svc.call("project.create", path=str(tmp_path / "wedding.mcsync"), name="Smith")
    assert info["name"] == "Smith" and info["settings"]["mode"] == "hybrid"
    imported = svc.run_job("media.import", paths=[str(shoot.root)])
    assert len(imported["clip_ids"]) == len(shoot.truth) and imported["problems"] == []
    progress = [n for n in svc.notifications() if n.get("method") == "job.progress"]
    assert progress and progress[-1]["params"]["progress"] == 1.0
    assert sum(n.get("method") == "media.imported" for n in svc.notifications()) == len(shoot.truth)

    listing = svc.call("media.list")
    clips = {Path(c["path"]).name: c for c in listing["clips"]}
    ref = clips["230614_001.WAV"]["clip_id"]
    assert clips["A001.MOV"]["frame_rate"] == "24000/1001" and clips["A001.MOV"]["timecode"]
    assert clips["GH020042.MP4"]["chapter"]["index"] == 1

    result = svc.run_job("sync.run", reference_clip_id=ref, timecode_jam_synced=True)
    assert result["matched"] == result["pairs"] > 0 and result["reused"] == 0
    timeline = by_name(result["timeline"])
    origin = timeline["230614_001.WAV"]["start_s"]
    for rel in shoot.truth:
        c = timeline[Path(rel).name]
        assert c["status"] == "synced", c
        assert c["start_s"] - origin == pytest.approx(shoot.expected(rel), abs=0.04), rel
    assert svc.call("timeline.get") == result["timeline"]

    # The editor drags A002 by hand, then changes their mind.
    a002 = clips["A002.MOV"]["clip_id"]
    moved = by_name(svc.call("correction.add", kind="offset", clip_id=a002, other_clip_id=ref, offset_s=200.0))
    assert moved["A002.MOV"]["method"] == "manual"
    assert moved["A002.MOV"]["start_s"] - moved["230614_001.WAV"]["start_s"] == pytest.approx(200.0)
    restored = by_name(svc.call("correction.undo"))
    assert restored["A002.MOV"]["method"] == "audio"
    assert by_name(svc.call("correction.redo"))["A002.MOV"]["method"] == "manual"
    svc.call("correction.undo")
    assert len(svc.call("correction.list")) == 1

    partners = {m["other_name"]: m for m in svc.call("sync.matches", clip_id=a002)}
    recorder = partners["230614_001.WAV"]
    assert recorder["status"] == "confident" and not recorder["rejected"]
    assert recorder["offset_s"] == pytest.approx(shoot.expected("CAM_A/A002.MOV"), abs=1e-3)
    svc.call("correction.add", kind="reject_pair", clip_id=a002, other_clip_id=ref)
    assert {m["other_name"]: m for m in svc.call("sync.matches", clip_id=a002)}["230614_001.WAV"]["rejected"]
    svc.call("correction.undo")

    snapped = svc.call("sync.snap", clip_id=a002, anchor_clip_id=ref, approx_offset_s=159.8, radius_s=2.0)
    assert snapped["offset_s"] == pytest.approx(shoot.expected("CAM_A/A002.MOV"), abs=1e-3)
    assert snapped["status"] == "confident"

    wave = svc.call("waveform.info", clip_id=ref)
    assert Path(wave["directory"], wave["files"]["1024"]).is_file() and wave["rate"] == 8000

    # Nothing changed: a second run reuses every match.
    again = svc.run_job("sync.run")
    assert again["reused"] == again["pairs"] and again["matched"] == 0

    # Excluding the recorder's only link makes the drone depend on timecode alone.
    drone = clips["DJI_0001.MP4"]["clip_id"]
    excluded = by_name(svc.call("correction.add", kind="exclude", clip_id=drone))
    assert excluded["DJI_0001.MP4"]["status"] == "unsynced"

    svc.call("device.update", device_id=clips["A001.MOV"]["device_id"], name="Cam A (Canon)")
    assert {c["device_name"] for c in svc.call("media.list")["clips"]} >= {"Cam A (Canon)"}
    with pytest.raises(RpcFailure):
        svc.call("clip.set_audio", clip_id=a002, stream_index=99)
    with pytest.raises(RpcFailure):
        svc.call("waveform.info", clip_id=drone)

    svc.call("project.close")
    reopened = svc.call("project.open", path=str(tmp_path / "wedding.mcsync"))
    assert reopened["clips"] == len(shoot.truth) and reopened["settings"]["timecode_jam_synced"] is True


def test_parallel_matching_through_the_service(tmp_path, wedding_shoot):
    s = InProcess(tmp_path, workers=2)
    try:
        s.call("project.create", path=str(tmp_path / "p.mcsync"))
        s.run_job("media.import", paths=[str(wedding_shoot.root)])
        result = s.run_job("sync.run", timecode_jam_synced=True)
        assert result["pairs"] >= 8 and result["matched"] == result["pairs"]
        assert result["timeline"]["stats"]["synced"] == len(wedding_shoot.truth)
    finally:
        s.service.close()


# ---------------------------------------------------------------------------
# Child process over stdio, like the desktop app
# ---------------------------------------------------------------------------


class Child:
    def __init__(self, cache: Path) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcsync.cli", "--cache-dir", str(cache), "--workers", "1", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
        )  # fmt: skip
        self.lines: queue.Queue[dict] = queue.Queue()
        self.notifications: list[dict] = []
        threading.Thread(target=self._read, daemon=True).start()
        self._id = 0

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            self.lines.put(json.loads(line))

    def call(self, method: str, **params):
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]
        while True:
            message = self.lines.get(timeout=120)
            if message.get("id") == self._id:
                assert "error" not in message, message
                return message["result"]
            self.notifications.append(message)

    def wait_for(self, predicate, timeout: float = 300) -> dict:
        for n in self.notifications:
            if predicate(n):
                self.notifications.remove(n)
                return n
        while True:
            message = self.lines.get(timeout=timeout)
            if predicate(message):
                return message
            self.notifications.append(message)


def test_child_process_protocol_and_resume_after_kill(tmp_path, wedding_shoot):
    project = str(tmp_path / "kill.mcsync")
    child = Child(tmp_path / "cache")
    assert child.call("engine.hello")["protocol"] == 1
    child.call("project.create", path=project)
    job = child.call("media.import", paths=[str(wedding_shoot.root)])["job_id"]
    child.wait_for(lambda m: m.get("method") == "job.done" and m["params"]["job_id"] == job)
    job = child.call("sync.run", timecode_jam_synced=True)["job_id"]
    # Kill the engine as soon as one pair match has been stored (hard crash, no cleanup).
    child.wait_for(lambda m: m.get("method") == "job.progress" and m["params"]["message"].startswith("Matched"))
    child.proc.kill()
    child.proc.wait(timeout=30)

    child = Child(tmp_path / "cache")
    info = child.call("project.open", path=project)
    assert info["clips"] == len(wedding_shoot.truth)
    job = child.call("sync.run")["job_id"]
    done = child.wait_for(lambda m: m.get("method") == "job.done" and m["params"]["job_id"] == job)
    result = done["params"]["result"]
    assert result["reused"] >= 1 and result["reused"] + result["matched"] == result["pairs"]
    assert result["timeline"]["stats"]["synced"] == len(wedding_shoot.truth)
    assert child.call("engine.shutdown") == {"ok": True}
    assert child.proc.wait(timeout=30) == 0


def test_command_line(tmp_path, wedding_shoot):
    def mcsync(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "mcsync.cli", "--cache-dir", str(tmp_path / "cache"), "--workers", "1", *args],
            capture_output=True, text=True, timeout=600,
        )  # fmt: skip

    probed = mcsync("probe", wedding_shoot.path("CAM_A/A001.MOV"))
    assert probed.returncode == 0
    assert json.loads(probed.stdout)["timecode"]["source"] == "tmcd"
    reference = wedding_shoot.path(wedding_shoot.reference)
    synced = mcsync("sync", str(wedding_shoot.root), "--reference", reference, "--jam-synced", "--json",
                    "--project", str(tmp_path / "cli.mcsync"))  # fmt: skip
    assert synced.returncode == 0, synced.stderr
    result = json.loads(synced.stdout)
    assert result["timeline"]["stats"]["synced"] == len(wedding_shoot.truth)
    table = mcsync("sync", str(wedding_shoot.root), "--project", str(tmp_path / "cli.mcsync"))
    assert table.returncode == 0 and "reused" in table.stdout and "A002.MOV" in table.stdout
    assert mcsync("sync", str(wedding_shoot.root), "--reference", "/not/there.wav").returncode == 2
