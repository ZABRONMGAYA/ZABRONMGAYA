"""The engine service: every JSON-RPC method the desktop app calls.

Method names and payloads are the IPC contract of docs/ARCHITECTURE.md §6.
Clip, device and correction ids are the project database's integer ids; the
sync engine sees clip ids as their decimal strings.

Work on media runs in the project's persistent pipeline (:mod:`mcsync.pipeline`):
importing, analysing and synchronising thousands of files happens in the
background while every other method stays responsive. ``media.import`` and
``sync.run`` are jobs that follow the pipeline to the end (for the command line
and simple clients); the app uses ``media.add`` / ``sync.start`` and the
``pipeline.progress`` notifications.

Synchronisation is incremental: every pair match is stored under a key made
from both signals' content fingerprints, the search window and the analysis
parameters. A run reuses every stored match whose key still applies, so adding
one camera to a synced project only matches that camera's pairs, and a run
that was cancelled or crashed resumes where it stopped.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from mcsync import __version__
from mcsync.export import ExportOptions, export_timeline
from mcsync.media.cache import AnalysisCache
from mcsync.media.extract import extract_to_cache
from mcsync.media.fingerprint import fingerprint
from mcsync.media.library import build_clip_inputs
from mcsync.media.tools import FFmpegNotFound, FFmpegTools, find_tools
from mcsync.media.waveform import PEAK_LEVELS
from mcsync.pipeline.discovery import volume_of, volume_online, walk
from mcsync.pipeline.runner import PRIORITY, Pipeline
from mcsync.project.db import ClipRow, Project, ProjectError
from mcsync.resources import (
    WorkerPlan,
    detect,
    disk_space,
    measure_write_speed,
    raise_open_file_limit,
    recommend_workers,
)
from mcsync.serialize import to_jsonable
from mcsync.sync.candidates import PlannedPair
from mcsync.sync.engine import SyncEngine, SyncOptions, create_match_pool
from mcsync.sync.params import DEFAULT_PARAMS, SyncParams
from mcsync.sync.types import ClipInput, PlacementMethod, PlacementStatus, SyncMode
from mcsync.timecode import parse_frame_rate
from mcsync.timeline import build_timeline

from .jobs import Job, JobCancelled, JobManager
from .rpc import APP_ERROR, BUSY, FFMPEG_MISSING, NO_PROJECT, JsonRpcServer, RpcError

PROTOCOL_VERSION = 2
#: Keys of clock-window and full-search matches (unchanged since release 0.1, so older projects keep their matches).
MATCH_KEY_VERSION = "2"
#: Keys of fingerprint-candidate and extended-search matches: independent of the exact window.
STAGED_KEY_VERSION = "3"
SETTINGS_DEFAULTS: dict[str, Any] = {
    "mode": SyncMode.HYBRID.value,
    "reference_clip_id": None,
    "timecode_jam_synced": False,
    "use_creation_time": True,
    "review_threshold": 0.85,
}
HIGH_CONFIDENCE = 0.95


class EngineService:
    """All engine methods. Attached to a :class:`JsonRpcServer` for the desktop app,
    or used directly (``server=None``) by the command line."""

    def __init__(
        self,
        server: JsonRpcServer | None = None,
        *,
        notify: Callable[[str, Any], None] | None = None,
        cache_dir: str | None = None,
        workers: int | None = None,
    ) -> None:
        self.server = server
        self.notify = server.notify if server is not None else (notify or (lambda method, params: None))
        self.cache = AnalysisCache(cache_dir)
        self.recommended = recommend_workers()
        self.worker_mode = "auto" if workers is None else "manual"
        self.plan = self.recommended if workers is None else _plan_for(workers, self.recommended)
        self.params: SyncParams = DEFAULT_PARAMS
        self.project: Project | None = None
        self.pipeline: Pipeline | None = None
        self.jobs = JobManager(self.notify, log=self.log)
        self._pool: Executor | None = None
        self._pool_lock = threading.Lock()
        self._tools: FFmpegTools | None = None
        raise_open_file_limit()
        if server is None:
            return
        server.error_mappers.append(self._map_error)
        for name in (
            "engine.hello", "engine.shutdown", "engine.configure", "system.resources", "system.disk_speed",
            "project.create", "project.open", "project.close", "project.info", "project.update_settings",
            "project.stats",
            "media.import", "media.add", "media.list", "media.index", "media.remove", "media.rescan",
            "media.duplicates", "media.decide_duplicates", "media.offline", "media.relink_folder",
            "media.relink_file", "media.ignore_offline",
            "device.update", "device.create", "clip.assign_device", "clip.set_audio",
            "session.list", "session.create", "session.assign",
            "pipeline.status", "pipeline.pause", "pipeline.resume", "pipeline.restart",
            "tasks.list", "tasks.cancel", "tasks.retry", "tasks.prioritize", "tasks.analyze",
            "sync.run", "sync.start", "sync.cancel", "sync.summary", "sync.solve", "sync.snap", "sync.matches",
            "correction.add", "correction.undo", "correction.redo", "correction.list",
            "timeline.get", "waveform.info", "export.xml",
            "cache.info", "cache.clear_unused",
            "job.cancel", "job.list",
        ):  # fmt: skip
            server.register(name, getattr(self, name.replace(".", "_")))

    @staticmethod
    def _map_error(exc: BaseException) -> RpcError | None:
        if isinstance(exc, ProjectError):
            return RpcError(APP_ERROR, str(exc))
        if isinstance(exc, FFmpegNotFound):
            return RpcError(FFMPEG_MISSING, str(exc))
        if isinstance(exc, (ValueError, KeyError)):
            return RpcError(APP_ERROR, str(exc))
        return None

    def log(self, text: str) -> None:
        print(text, file=sys.stderr)

    def _require_project(self) -> Project:
        if self.project is None:
            raise RpcError(NO_PROJECT, "no project is open")
        return self.project

    def _pipeline(self) -> Pipeline:
        self._require_project()
        assert self.pipeline is not None
        return self.pipeline

    def _require_idle(self, kinds: set[str]) -> None:
        busy = self.jobs.running(kinds)
        if busy:
            raise RpcError(BUSY, f"a {busy[0].kind} job is running", {"job_id": busy[0].id})

    def tools(self) -> FFmpegTools:
        if self._tools is None:
            self._tools = find_tools()
        return self._tools

    # ------------------------------------------------------------ workers

    @property
    def match_workers(self) -> int:
        return max(1, self.plan.match)

    def match_pool(self) -> Executor:
        """Processes that match audio (one thread in-process when a single worker is configured)."""
        with self._pool_lock:
            if self._pool is None:
                n = self.match_workers
                self._pool = ThreadPoolExecutor(1, thread_name_prefix="match") if n <= 1 else create_match_pool(n)
            return self._pool

    def reset_match_pool(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None

    def close(self) -> None:
        self.jobs.cancel_all()
        for job in list(self.jobs.jobs.values()):
            job.finished.wait(10)
        self._close_project()
        self.reset_match_pool()

    def _close_project(self) -> None:
        if self.pipeline is not None:
            self.pipeline.stop()
            self.pipeline = None
        if self.project is not None:
            self.project.close()
            self.project = None

    # ---------------------------------------------------------------- engine

    def engine_hello(self, client: str | None = None) -> dict:
        try:
            ffmpeg = self.tools().version()
        except FFmpegNotFound:
            ffmpeg = None
        return {
            "name": "mcsync-engine",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "ffmpeg": ffmpeg,
            "cache_dir": str(self.cache.root),
            "workers": self.match_workers,
            "plan": self.plan.to_dict(),
            "worker_mode": self.worker_mode,
        }

    def engine_shutdown(self) -> dict:
        self.close()
        if self.server is not None:
            self.server.stop()
        return {"ok": True}

    def engine_configure(self, workers: dict | str | None = None) -> dict:
        """Worker counts: ``"auto"`` (recommended for this computer) or ``{"probe", "analyze", "match"}``."""
        if workers is not None:
            if workers == "auto":
                self.recommended = recommend_workers()
                plan, self.worker_mode = self.recommended, "auto"
            elif isinstance(workers, dict):
                unknown = set(workers) - {"probe", "analyze", "match"}
                if unknown:
                    raise RpcError(APP_ERROR, f"unknown worker kinds: {sorted(unknown)}")
                values = {k: int(workers.get(k, getattr(self.plan, k))) for k in ("probe", "analyze", "match")}
                if any(v < 1 or v > 256 for v in values.values()):
                    raise RpcError(APP_ERROR, "worker counts must be between 1 and 256")
                plan, self.worker_mode = WorkerPlan(**values, reason="set by the user"), "manual"
            else:
                raise RpcError(APP_ERROR, "workers must be 'auto' or an object")
            if plan.match != self.plan.match:
                self.reset_match_pool()
            self.plan = plan
            if self.pipeline is not None:
                self.pipeline.set_plan(plan)
        return {"plan": self.plan.to_dict(), "mode": self.worker_mode, "recommended": self.recommended.to_dict()}

    def system_resources(self) -> dict:
        res = detect()
        self.recommended = recommend_workers(res)
        project_dir = str(self.project.path.parent) if self.project is not None else None
        return {
            "resources": res.to_dict(),
            "recommended": self.recommended.to_dict(),
            "plan": self.plan.to_dict(),
            "mode": self.worker_mode,
            "gpu_used": False,
            "storage": {
                "cache": disk_space(self.cache.root),
                "project": disk_space(project_dir) if project_dir else None,
            },
        }

    def system_disk_speed(self, path: str | None = None) -> dict:
        target = Path(path) if path else self.cache.root
        return {"path": str(target), "write_bytes_per_s": measure_write_speed(target)}

    # --------------------------------------------------------------- project

    def _project_summary(self) -> dict:
        project = self._require_project()
        stats = project.refresh_media_status()
        pipeline = self._pipeline()
        counts = project.task_counts()
        pending = {k: v["pending"] + v["running"] for k, v in counts.items() if v["pending"] + v["running"]}
        sync = pipeline.sync_state
        unfinished_sync = sync if sync is not None and sync.get("phase") != "done" else None
        return {
            "path": str(project.path),
            "name": project.name,
            "settings": {**SETTINGS_DEFAULTS, **project.settings()},
            "clips": project.clip_count(),
            "offline": sum(n for status, n in stats.items() if status != "online"),
            "devices": len(project.devices()),
            "last_run": project.last_completed_run(),
            "resume": {"pending": pending, "sync": unfinished_sync} if pending or unfinished_sync else None,
            "paused": pipeline.paused,
        }

    def _attach(self, project: Project) -> None:
        self.project = project
        counts = project.task_counts()
        leftover = any(v["pending"] for v in counts.values())
        sync = project.meta_get("sync")
        leftover = leftover or (sync is not None and sync.get("phase") != "done")
        # Work left from a previous session waits for the user (resume or restart); new projects start ready.
        self.pipeline = Pipeline(self, project, self.plan, start_paused=leftover)
        self.pipeline.start()

    def project_create(self, path: str, name: str | None = None) -> dict:
        self._require_idle({"import", "sync"})
        self._close_project()
        self._attach(Project.create(path, name))
        return self._project_summary()

    def project_open(self, path: str) -> dict:
        self._require_idle({"import", "sync"})
        self._close_project()
        self._attach(Project.open(path))
        return self._project_summary()

    def project_close(self) -> dict:
        self._require_idle({"import", "sync"})
        self._close_project()
        return {"ok": True}

    def project_info(self) -> dict:
        return self._project_summary()

    def project_update_settings(self, **settings: Any) -> dict:
        unknown = set(settings) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise RpcError(APP_ERROR, f"unknown settings: {sorted(unknown)}")
        if "mode" in settings:
            SyncMode(settings["mode"])
        if "review_threshold" in settings and not 0.0 < float(settings["review_threshold"]) <= 1.0:
            raise RpcError(APP_ERROR, "review_threshold must be within (0, 1]")
        project = self._require_project()
        project.update_settings(**settings)
        return {**SETTINGS_DEFAULTS, **project.settings()}

    def project_stats(self) -> dict:
        project = self._require_project()
        stats = project.database_stats()
        return {**stats, "wal_bytes": stats["wal_bytes"], "path": str(project.path)}

    # ----------------------------------------------------------------- media

    def media_add(self, paths: list[str], recursive: bool = True, priority: str = "normal") -> dict:
        """Import in the background: files are counted as they are found and processed as they arrive."""
        self.tools()  # fail now if FFmpeg is missing
        pipeline = self._pipeline()
        pipeline.resume()
        return {"request_id": pipeline.import_paths(paths, recursive=recursive, priority=PRIORITY[priority])}

    def media_import(self, paths: list[str], recursive: bool = True) -> dict:
        """Import and analyse, as a job that ends when every file found is read and analysed."""
        project = self._require_project()
        self.tools()
        pipeline = self._pipeline()
        pipeline.resume()
        request_id = pipeline.import_paths(paths, recursive=recursive)

        def work(job: Job) -> dict:
            while True:
                if job.cancel.is_set():
                    req = pipeline.import_request(request_id)
                    pipeline.cancel(kinds=["probe", "analyze"])
                    raise JobCancelled()
                req = pipeline.import_request(request_id)
                counts = project.task_counts()
                probe, analyze = counts.get("probe", {}), counts.get("analyze", {})
                busy = sum(probe.get(k, 0) + analyze.get(k, 0) for k in ("pending", "running"))
                total = sum(probe.values()) + sum(analyze.values())
                if not req.walking and busy == 0 and not pipeline.paused:
                    snapshot_busy = any(r["kind"] in ("probe", "analyze") for r in pipeline.snapshot()["running"])
                    if not snapshot_busy:
                        break
                message = f"Found {req.found} files" if req.walking else f"Analysed {total - busy} of {total}"
                job.report(0.99 * (total - busy) / total if total else 0.0, message)
                time.sleep(0.1)
            found = project.discovered(req.discovered_ids)
            media_ids = [d["media_id"] for d in found.values() if d["media_id"] is not None]
            clip_of = {r.media_id: r.id for r in project.clips()}
            problems = [{"path": p.path, "message": p.message} for p in req.problems]
            problems += [{"path": d["path"], "message": d["error"] or d["status"]} for d in found.values()
                         if d["status"] in ("failed", "skipped")]  # fmt: skip
            return {"clip_ids": [clip_of[m] for m in media_ids if m in clip_of], "problems": problems}

        return {"job_id": self.jobs.start("import", work).id}

    def _clip_summary(self, row: ClipRow) -> dict:
        info = row.info
        return {
            "clip_id": row.id,
            "name": row.name,
            "path": row.path,
            "status": row.status,
            "device_id": row.device_id,
            "device_name": row.device_name,
            "kind": row.device_kind,
            "duration_s": info.duration_s,
            "frame_rate": f"{info.frame_rate.numerator}/{info.frame_rate.denominator}" if info.frame_rate else None,
            "vfr": any(v.is_vfr for v in info.video),
            "timecode": info.timecode.text if info.timecode else None,
            "creation_time": info.creation_time.isoformat() if info.creation_time else None,
            "audio_streams": [
                {"index": a.index, "channels": a.channels, "codec": a.codec, "sample_rate": a.sample_rate}
                for a in info.audio
            ],  # fmt: skip
            "audio_stream": row.audio_stream,
            "audio_channel": row.audio_channel,
            "chapter": {"take": row.chapter_take, "index": row.chapter_index} if row.chapter_take else None,
            "session_id": row.session_id,
            "duplicate_of": row.duplicate_of,
            "duplicate_decision": row.duplicate_decision,
        }

    def media_list(self) -> dict:
        project = self._require_project()
        return {"clips": [self._clip_summary(r) for r in project.clips()], "devices": project.devices()}

    def media_index(self) -> dict:
        """Every clip as one compact row (column names + value arrays) with its pipeline and sync state: what large
        lists, filters and searches need, without per-clip JSON."""
        project = self._require_project()
        rows = project.media_rows()
        tasks = project.clip_task_status()
        analysis = project.audio_analysis()
        placements = project.placements()
        threshold = float(self._settings()["review_threshold"])
        excluded = {int(c) for c in project.corrections().excluded_clips}
        out_rows = []
        for r in rows:
            cid = r["clip_id"]
            p = placements.get(cid)
            t = tasks.get(cid, {})
            a = analysis.get(cid)
            category = _category(r, p, a, t, cid in excluded, threshold)
            out_rows.append([
                cid, r["name"], r["kind"], r["device_id"], r["device_name"], r["device_kind"], r["session_id"],
                r["duration_s"], r["fps"], r["width"], r["height"], r["codec"], r["sample_rate"], r["channels"],
                r["timecode"], r["creation_time"], r["size_bytes"], r["status"], r["duplicate_of"],
                r["duplicate_decision"], t.get("probe"), a["status"] if a else t.get("analyze"),
                p.status.value if p else None, p.confidence if p else None, p.method.value if p else None,
                p.start_s if p else None, p.group if p else None, category, r["path"],
            ])  # fmt: skip
        return {
            "columns": [
                "clip_id",
                "name",
                "kind",
                "device_id",
                "device_name",
                "device_kind",
                "session_id",
                "duration_s",
                "fps",
                "width",
                "height",
                "codec",
                "sample_rate",
                "channels",
                "timecode",
                "creation_time",
                "size_bytes",
                "media_status",
                "duplicate_of",
                "duplicate_decision",
                "probe",
                "analysis",
                "sync_status",
                "confidence",
                "method",
                "start_s",
                "group",
                "category",
                "path",
            ],  # fmt: skip
            "rows": out_rows,
            "devices": project.devices(),
            "sessions": project.sessions(),
            "version": project.clip_version,
        }

    def media_remove(self, clip_ids: list[int]) -> dict:
        """Remove clips from the project. Files on disk are never touched."""
        self._require_idle({"import", "sync"})
        self._pipeline().cancel(clip_ids=clip_ids)
        self._require_project().remove_clips(clip_ids)
        return {"removed": len(clip_ids)}

    def media_rescan(self) -> dict:
        """Look for new, changed and missing files under the folders imported before (in the background)."""
        pipeline = self._pipeline()

        def work(job: Job) -> dict:
            return pipeline.rescan()

        return {"job_id": self.jobs.start("rescan", work).id}

    def media_duplicates(self) -> list[dict]:
        return self._require_project().duplicates()

    def media_decide_duplicates(self, media_ids: list[int], decision: str) -> dict:
        """``keep`` both (the copy is synchronised too) or ``ignore`` the copy (left out). Nothing is deleted."""
        project = self._require_project()
        project.decide_duplicates(media_ids, decision)
        return {"duplicates": project.duplicates()}

    def media_offline(self) -> dict:
        """Media whose file is missing or changed, grouped by drive."""
        project = self._require_project()
        project.refresh_media_status()
        ignored = set(project.meta_get("offline_ignored", []))
        groups: dict[str, list[dict]] = defaultdict(list)
        for m in project.offline_media():
            groups[volume_of(m["path"])].append(m)
        return {
            "volumes": [
                {"volume": v, "online": volume_online(v), "ignored": v in ignored, "count": len(items), "media": items}
                for v, items in sorted(groups.items())
            ]  # fmt: skip
        }

    def media_ignore_offline(self, volume: str, ignore: bool = True) -> dict:
        project = self._require_project()
        ignored = set(project.meta_get("offline_ignored", []))
        (ignored.add if ignore else ignored.discard)(volume)
        project.meta_set("offline_ignored", sorted(ignored))
        return self.media_offline()

    def media_relink_folder(self, folder: str, recursive: bool = True) -> dict:
        """Find offline media in ``folder`` by name, size and content fingerprint; relink every one that matches."""
        project = self._require_project()
        missing = {}
        for m in project.offline_media():
            missing.setdefault((m["filename"], m["size_bytes"]), []).append(m)
        wanted = {r.media_id: r.fingerprint for r in project.clips()}
        moves, mismatched = [], []
        for batch in walk([folder], recursive=recursive):
            for f in batch:
                name = Path(f.path).name
                for m in missing.get((name, f.size), []):
                    if any(m["media_id"] == mv[0] for mv in moves):
                        continue
                    if fingerprint(f.path) == wanted.get(m["media_id"]):
                        moves.append((m["media_id"], f.path, f.size, f.mtime_ns))
                        break
                    mismatched.append(f.path)
        project.relink(moves)
        self._pipeline().analyze()
        return {"relinked": len(moves), "not_matching": mismatched, **self.media_offline()}

    def media_relink_file(self, media_id: int, path: str, force: bool = False) -> dict:
        """Point one media file at a new location. The content must match unless ``force``."""
        project = self._require_project()
        row = next((r for r in project.clips() if r.media_id == media_id), None)
        if row is None:
            raise RpcError(APP_ERROR, f"no media {media_id}")
        st = os.stat(path)
        if not force and fingerprint(path) != row.fingerprint:
            raise RpcError(APP_ERROR, f"{Path(path).name} is not the same recording as {row.name}")
        project.relink([(media_id, path, st.st_size, st.st_mtime_ns)])
        if force:
            self._pipeline().import_paths([path])
        return self.media_offline()

    def device_update(self, device_id: int, name: str | None = None, kind: str | None = None) -> dict:
        self._require_project().update_device(device_id, name=name, kind=kind)
        return {"devices": self._require_project().devices()}

    def device_create(self, name: str, kind: str = "camera") -> dict:
        return {"device_id": self._require_project().create_device(name, kind)}

    def clip_assign_device(self, clip_id: int | None = None, device_id: int | None = None,
                           clip_ids: list[int] | None = None) -> dict:  # fmt: skip
        ids = clip_ids if clip_ids is not None else [clip_id]
        if device_id is None or not ids or None in ids:
            raise RpcError(APP_ERROR, "clip_ids and device_id are required")
        self._require_project().set_clips_device(ids, device_id)  # type: ignore[arg-type]
        return {"assigned": len(ids), "devices": self._require_project().devices()}

    def clip_set_audio(self, clip_id: int, stream_index: int | None, channel: int | None = None) -> dict:
        project = self._require_project()
        row = project.clip(clip_id)
        if stream_index is not None:
            stream = next((a for a in row.info.audio if a.index == stream_index), None)
            if stream is None:
                raise RpcError(APP_ERROR, f"clip {clip_id} has no audio stream {stream_index}")
            if channel is not None and not 0 <= channel < stream.channels:
                raise RpcError(APP_ERROR, f"stream {stream_index} has no channel {channel}")
        project.set_clip_audio(clip_id, stream_index, channel)
        if stream_index is not None:
            self._pipeline().analyze([clip_id], priority=PRIORITY["high"])
        return self._clip_summary(project.clip(clip_id))

    def session_list(self) -> list[dict]:
        return self._require_project().sessions()

    def session_create(self, label: str, clip_ids: list[int]) -> dict:
        return {"session_id": self._require_project().create_session(label, clip_ids)}

    def session_assign(self, clip_ids: list[int], session_id: int | None) -> dict:
        self._require_project().assign_session(clip_ids, session_id)
        return {"sessions": self._require_project().sessions()}

    # --------------------------------------------------------------- pipeline

    def pipeline_status(self) -> dict:
        return self._pipeline().snapshot()

    def pipeline_pause(self) -> dict:
        self._pipeline().pause()
        return self._pipeline().snapshot()

    def pipeline_resume(self) -> dict:
        self._pipeline().resume()
        return self._pipeline().snapshot()

    def pipeline_restart(self, sync: bool = False) -> dict:
        """Drop the unfinished work of a previous session and start again (analysis already cached is reused)."""
        pipeline = self._pipeline()
        pipeline.cancel_sync()
        pipeline.cancel(kinds=["match", "extend"])
        self._require_project().retry_tasks(statuses=["pending", "cancelled", "failed"], kinds=["probe", "analyze"])
        pipeline.resume()
        if sync:
            self.sync_start()
        return pipeline.snapshot()

    def tasks_list(self, statuses: list[str], kinds: list[str] | None = None, limit: int = 200,
                   offset: int = 0) -> list[dict]:  # fmt: skip
        return self._require_project().tasks(statuses=statuses, kinds=kinds, limit=limit, offset=offset)

    def tasks_cancel(self, ids: list[int] | None = None, clip_ids: list[int] | None = None,
                     kinds: list[str] | None = None, running: bool = True) -> dict:  # fmt: skip
        return {"cancelled": self._pipeline().cancel(ids=ids, clip_ids=clip_ids, kinds=kinds, running=running)}

    def tasks_retry(self, statuses: list[str] | None = None, ids: list[int] | None = None,
                    clip_ids: list[int] | None = None, kinds: list[str] | None = None) -> dict:  # fmt: skip
        pipeline = self._pipeline()
        n = pipeline.retry(statuses=statuses or ["failed"], ids=ids, clip_ids=clip_ids, kinds=kinds)
        pipeline.resume()
        return {"retried": n}

    def tasks_prioritize(self, clip_ids: list[int], priority: str = "high") -> dict:
        return {"changed": self._pipeline().prioritize(clip_ids, PRIORITY[priority])}

    def tasks_analyze(self, clip_ids: list[int] | None = None, priority: str = "normal") -> dict:
        """(Re-)analyse the audio of these clips, or of every clip not analysed yet."""
        return {"queued": self._pipeline().analyze(clip_ids, priority=PRIORITY[priority])}

    # ------------------------------------------------------------------ sync

    def _settings(self) -> dict:
        return {**SETTINGS_DEFAULTS, **self._require_project().settings()}

    def _clip_inputs(self, rows: list[ClipRow], job: Job | None = None, *, extract: bool = True) -> list[ClipInput]:
        """Engine inputs for the project's clips. Signals are mapped lazily; with ``extract``, audio not analysed
        yet is extracted first (otherwise such clips count as having no audio)."""
        settings = self._settings()
        items = []
        for k, row in enumerate(rows):
            item = row.to_media_item()
            if item.audio_stream is not None:
                entry = self.cache.entry(row.fingerprint, item.audio_stream.index, channel=row.audio_channel,
                                         params=self.params)  # fmt: skip
                if entry.exists():
                    item.signal = entry.load()
                elif extract and row.status != "offline":
                    if job is not None:
                        job.report(0.1 * k / len(rows), f"Extracting audio from {row.name}")
                    item.signal = extract_to_cache(row.info, entry, stream=item.audio_stream,
                                                   channel=row.audio_channel, params=self.params,
                                                   cancel=job.cancel if job else None)  # fmt: skip
            items.append(item)
        return build_clip_inputs(
            items,
            timecode_jam_synced=bool(settings["timecode_jam_synced"]),
            use_creation_time=bool(settings["use_creation_time"]),
            clip_ids=[r.engine_id for r in rows],
        )

    def _engine(self) -> SyncEngine:
        settings = self._settings()
        ref = settings.get("reference_clip_id")
        return SyncEngine(
            SyncOptions(
                mode=SyncMode(settings["mode"]),
                reference_clip_id=str(ref) if ref is not None else None,
                params=self.params,
            )  # fmt: skip
        )

    def pair_key(self, pair: PlannedPair, rows: dict[str, ClipRow], inputs: dict[str, ClipInput]) -> str:
        """Identity of a pair match: both recordings' content, the search and the analysis parameters."""

        def side(clip_id: str) -> list:
            row, clip = rows[clip_id], inputs[clip_id]
            return [row.fingerprint, row.audio_stream, row.audio_channel, round(clip.audio_start_s, 6)]

        if pair.stage in ("clock", "full"):
            (window,) = pair.windows if len(pair.windows) == 1 else (tuple(pair.windows),)
            w = None if window is None else [round(window[0], 3), round(window[1], 3)]
            text = json.dumps([MATCH_KEY_VERSION, repr(self.params), side(pair.ref), side(pair.tgt), w])
        else:
            text = json.dumps([STAGED_KEY_VERSION, pair.stage, repr(self.params), side(pair.ref), side(pair.tgt)])
        return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()

    def sync_start(self, mode: str | None = None, reference_clip_id: int | None = None,
                   timecode_jam_synced: bool | None = None) -> dict:  # fmt: skip
        """Synchronise every clip in the background (after metadata and audio analysis still running)."""
        changes = {"mode": mode, "reference_clip_id": reference_clip_id, "timecode_jam_synced": timecode_jam_synced}
        self.project_update_settings(**{k: v for k, v in changes.items() if v is not None})
        run_id = self._pipeline().start_sync({"settings": self._settings(), "params": repr(self.params)})
        return {"run_id": run_id}

    def sync_cancel(self) -> dict:
        return {"cancelled": self._pipeline().cancel_sync()}

    def sync_run(
        self, mode: str | None = None, reference_clip_id: int | None = None, timecode_jam_synced: bool | None = None
    ) -> dict:
        """Synchronise, as a job that ends with the timeline."""
        project = self._require_project()
        self._require_idle({"sync"})
        pipeline = self._pipeline()
        run_id = self.sync_start(mode, reference_clip_id, timecode_jam_synced)["run_id"]
        finished = threading.Event()
        outcome: dict = {}

        def listener(event: str, data: dict) -> None:
            if event == "sync_finished" and data.get("run_id") == run_id:
                outcome.update(data)
                finished.set()

        pipeline.add_listener(listener)

        def work(job: Job) -> dict:
            try:
                while not finished.wait(0.1):
                    if job.cancel.is_set():
                        pipeline.cancel_sync()
                        raise JobCancelled()
                    state = pipeline.sync_state or {}
                    counts = project.task_counts(run_id=run_id)
                    m, e = counts.get("match", {}), counts.get("extend", {})
                    done = m.get("done", 0) + e.get("done", 0) + m.get("failed", 0) + e.get("failed", 0)
                    total = done + sum(x.get(k, 0) for x in (m, e) for k in ("pending", "running"))
                    if done:
                        job.report(0.1 + 0.85 * done / max(total, 1), f"Matched {done} of {total} pairs")
                    else:
                        job.report(0.05, {"waiting": "Analysing audio", "planning": "Finding candidate pairs"}.get(
                            state.get("phase", ""), "Synchronising"))  # fmt: skip
            finally:
                pipeline.remove_listener(listener)
            if outcome.get("status") != "completed":
                raise RuntimeError(outcome.get("error") or f"sync {outcome.get('status')}")
            state = pipeline.sync_state or {}
            counts = project.task_counts(run_id=run_id)
            matched = sum(counts.get(k, {}).get("done", 0) for k in ("match", "extend"))
            reused = len(project.run_match_summaries(run_id)) - matched
            return {"run_id": run_id, "pairs": matched + reused, "reused": reused, "matched": matched,
                    "strategy": state.get("strategy"), "timeline": outcome.get("timeline")}  # fmt: skip

        return {"job_id": self.jobs.start("sync", work).id}

    def _solve(self, clips: list[ClipInput] | None = None) -> dict:
        project = self._require_project()
        rows = project.clips()
        if not rows:
            return to_jsonable(build_timeline([], {}))
        if clips is None:
            clips = self._clip_inputs(rows, extract=False)
        run_id = project.last_completed_run()
        matches = project.run_matches(run_id) if run_id is not None else []
        current = {c.clip_id for c in clips}
        matches = [m for m in matches if m.ref_id in current and m.tgt_id in current]
        corrections = project.corrections()
        ignored = {r.engine_id for r in rows if r.ignored_duplicate}
        if ignored:
            corrections = type(corrections)(
                offsets=[o for o in corrections.offsets if not {o.clip_id, o.anchor_clip_id} & ignored],
                rejected_pairs=corrections.rejected_pairs,
                excluded_clips=set(corrections.excluded_clips) | ignored,
            )
        result = self._engine().solve(clips, matches, corrections)
        project.save_placements(result)
        placements = {int(cid): p for cid, p in result.placements.items()}
        return to_jsonable(build_timeline(rows, placements, int(result.reference_id)))

    def sync_solve(self) -> dict:
        self._require_idle({"sync"})
        return self._solve()

    def sync_summary(self) -> dict:
        """Mass sync results: how many clips fall in each category, per source and overall."""
        index = self.media_index()
        cols = {c: k for k, c in enumerate(index["columns"])}
        categories = ("synchronized", "high_confidence", "review", "manual", "failed", "skipped", "pending")
        total = Counter()
        per_device: dict[int | None, Counter] = defaultdict(Counter)
        conf: dict[int | None, list[float]] = defaultdict(list)
        for row in index["rows"]:
            cat = row[cols["category"]]
            dev = row[cols["device_id"]]
            total[cat] += 1
            per_device[dev][cat] += 1
            if cat == "high_confidence":
                total["synchronized"] += 1
                per_device[dev]["synchronized"] += 1
            if row[cols["confidence"]] is not None and cat not in ("skipped", "failed"):
                conf[dev].append(float(row[cols["confidence"]]))
        devices = {d["id"]: d for d in index["devices"]}
        sources = []
        for dev, counts in per_device.items():
            d = devices.get(dev, {})
            values = sorted(conf[dev])
            sources.append({
                "device_id": dev, "name": d.get("name", "Unassigned"), "kind": d.get("kind"),
                "clips": sum(counts[c] for c in categories if c != "synchronized"),
                "counts": {c: counts[c] for c in categories},
                "median_confidence": values[len(values) // 2] if values else None,
                "min_confidence": values[0] if values else None,
            })  # fmt: skip
        sources.sort(key=lambda s: (s["kind"] == "recorder", s["name"]))
        project = self._require_project()
        state = self._pipeline().sync_state
        unreadable = project.discovery_counts()["by_status"].get("failed", 0)
        total["failed"] += unreadable
        return {
            "clips": len(index["rows"]),
            "unreadable_files": unreadable,
            "counts": {c: total[c] for c in categories},
            "sources": sources,
            "sessions": project.sessions(),
            "run": state,
            "last_run": project.last_completed_run(),
            "threshold": float(self._settings()["review_threshold"]),
        }

    def sync_snap(self, clip_id: int, anchor_clip_id: int, approx_offset_s: float, radius_s: float = 2.0) -> dict:
        """Refine a roughly dragged position by audio, within ±radius (not saved)."""
        project = self._require_project()
        rows = [project.clip(anchor_clip_id), project.clip(clip_id)]
        anchor, clip = self._clip_inputs(rows)
        if anchor.audio is None or clip.audio is None:
            raise RpcError(APP_ERROR, "both clips need audio to snap")
        match = self._engine().match_pair(anchor, clip, window=(approx_offset_s - radius_s, approx_offset_s + radius_s))
        return {
            "offset_s": match.offset_s,
            "confidence": match.confidence,
            "status": match.status.value,
            "flags": [f.value for f in match.all_flags],
            "alternatives": to_jsonable(match.alternatives),
        }

    def sync_matches(self, clip_id: int) -> list[dict]:
        """The last run's audio matches involving a clip, from that clip's point of view."""
        project = self._require_project()
        run_id = project.last_completed_run()
        if run_id is None:
            return []
        names = {r.id: r.name for r in project.clips()}
        rejected = project.corrections().rejected_pairs
        me = str(clip_id)
        out = []
        for m in project.clip_matches(run_id, clip_id):
            if m.status.value == "no_match":
                continue
            other = m.tgt_id if m.ref_id == me else m.ref_id
            offset = m.offset_s
            if offset is not None and m.ref_id == me:
                offset = -offset  # start(me) - start(other)
            drift = m.estimate.drift_ppm if m.tgt_id == me else -m.estimate.drift_ppm
            out.append({
                "other_clip_id": int(other),
                "other_name": names.get(int(other), other),
                "offset_s": offset,
                "confidence": m.confidence,
                "status": m.status.value,
                "flags": [f.value for f in m.all_flags],
                "drift_ppm": drift or 0.0,
                "rejected": frozenset((m.ref_id, m.tgt_id)) in rejected,
            })  # fmt: skip
        return sorted(out, key=lambda d: -d["confidence"])

    # ----------------------------------------------------------- corrections

    def correction_add(
        self, kind: str, clip_id: int, other_clip_id: int | None = None, offset_s: float | None = None
    ) -> dict:
        self._require_idle({"sync"})
        self._require_project().add_correction(kind, clip_id, other_clip_id=other_clip_id, offset_s=offset_s)
        return self._solve()

    def correction_undo(self) -> dict:
        self._require_idle({"sync"})
        self._require_project().undo()
        return self._solve()

    def correction_redo(self) -> dict:
        self._require_idle({"sync"})
        self._require_project().redo()
        return self._solve()

    def correction_list(self) -> list[dict]:
        return self._require_project().correction_log()

    # -------------------------------------------------------------- timeline

    def timeline_get(self) -> dict:
        project = self._require_project()
        placements = project.placements()
        ref = self._settings().get("reference_clip_id")
        return to_jsonable(build_timeline(project.clips(), placements, ref))

    # ---------------------------------------------------------------- export

    def export_xml(
        self,
        format: str,
        path: str,
        sequence_rate: str | None = None,
        start_timecode: str = "01:00:00:00",
        group: int = 0,
        include_uncertain: bool = True,
        name: str | None = None,
    ) -> dict:
        """Write the synchronised timeline as FCP 7 XML ("xmeml") or FCPXML ("fcpxml"); return the report."""
        self._require_idle({"sync"})
        project = self._require_project()
        rows = project.clips()
        timeline = build_timeline(rows, project.placements(), self._settings().get("reference_clip_id"))
        options = ExportOptions(
            name=name or project.name,
            sequence_rate=parse_frame_rate(sequence_rate) if sequence_rate else None,
            start_timecode=start_timecode,
            group=group,
            include_uncertain=include_uncertain,
        )
        report = export_timeline(timeline, {r.id: r for r in rows}, format, path, options)
        project.record_export(format, path, report["sequence"]["rate"], report)
        return report

    def waveform_info(self, clip_id: int) -> dict:
        """Where the clip's waveform overview lives; the app reads the files directly."""
        row = self._require_project().clip(clip_id)
        if row.audio_stream is None:
            raise RpcError(APP_ERROR, f"clip {clip_id} has no audio")
        entry = self.cache.entry(row.fingerprint, row.audio_stream, channel=row.audio_channel, params=self.params)
        if not entry.exists():
            raise RpcError(APP_ERROR, f"clip {clip_id} has not been analysed yet")
        meta = entry.meta()
        info = row.info
        return {
            "directory": str(entry.directory),
            "files": {str(spb): f"peaks_{spb}.i8" for spb in PEAK_LEVELS},
            "encoding": "mulaw-int8-minmax",
            "rate": meta["rate"],
            "samples": meta["samples"],
            "level_dbfs": meta["level_dbfs"],
            "audio_start_s": info.audio_start_s(row.audio_stream_info),
        }

    # ------------------------------------------------------------------ jobs

    def job_cancel(self, job_id: str) -> dict:
        return {"cancelled": self.jobs.cancel(job_id)}

    def job_list(self) -> list[dict]:
        return [j.summary() for j in self.jobs.jobs.values()]

    # ----------------------------------------------------------------- cache

    def _project_cache_dirs(self) -> set[Path]:
        if self.project is None:
            return set()
        return {Path(a["cache_key"]) for a in self.project.audio_analysis().values() if a["cache_key"]}

    def cache_info(self) -> dict:
        """Size of the analysis cache, how much of it the open project uses, and the space left on its drive."""
        keep = self._project_cache_dirs()
        total = used = count = 0
        for entry in self.cache.entries():
            size = entry.size_bytes()
            total += size
            count += 1
            if entry.directory in keep:
                used += size
        return {"root": str(self.cache.root), "bytes": total, "entries": count, "project_bytes": used,
                "disk": disk_space(self.cache.root)}  # fmt: skip

    def cache_clear_unused(self) -> dict:
        """Delete cached analysis that the open project does not use (it is recomputed if ever needed again)."""
        keep = self._project_cache_dirs()
        budget, self.cache.max_bytes = self.cache.max_bytes, 0
        try:
            freed = self.cache.evict(keep=frozenset(keep))
        finally:
            self.cache.max_bytes = budget
        return {"freed_bytes": freed, **self.cache_info()}


def _plan_for(workers: int, recommended: WorkerPlan) -> WorkerPlan:
    """A worker plan for an explicit matcher count (``--workers``)."""
    workers = max(1, int(workers))
    return WorkerPlan(probe=recommended.probe, analyze=max(1, min(recommended.analyze, workers)), match=workers,
                      reason=f"{workers} matcher worker(s) requested")  # fmt: skip


def _category(row: dict, placement, analysis: dict | None, tasks: dict, excluded: bool, threshold: float) -> str:  # noqa: ANN001
    """Where a clip stands in the mass-sync results (``sync.summary``)."""
    if excluded or (row["duplicate_of"] is not None and row["duplicate_decision"] != "keep"):
        return "skipped"
    if tasks.get("probe") == "failed" or (analysis is not None and analysis["status"] == "failed"):
        return "failed"
    if placement is None:
        if analysis is not None and analysis["status"] == "skipped" and row["status"] != "online":
            return "skipped"
        return "pending"
    if placement.status == PlacementStatus.UNSYNCED:
        return "skipped" if placement.method == PlacementMethod.NONE and "excluded" in {
            f.value for f in placement.flags} else "manual"  # fmt: skip
    if placement.method in (PlacementMethod.REFERENCE, PlacementMethod.MANUAL):
        return "high_confidence" if placement.method == PlacementMethod.REFERENCE else "synchronized"
    if placement.status == PlacementStatus.NEEDS_REVIEW or placement.confidence < threshold:
        return "review"
    return "high_confidence" if placement.confidence >= HIGH_CONFIDENCE else "synchronized"


def _claim_stdio() -> tuple[int, int]:
    """Take stdin/stdout for the protocol; give the rest of the process harmless standard streams.

    The protocol uses private duplicates. Descriptor 0 then reads the null device and descriptor 1 writes to stderr,
    so stray prints land in the log and child processes (FFmpeg, the matcher pool) can neither read requests nor
    write into the protocol stream. On Windows this also avoids a deadlock: a starting Python process queries its
    standard input, and on a pipe this process is blocked reading, that query waits for the next request. The
    matcher pool never started while the app waited for a sync to finish.
    """
    protocol_in, protocol_out = os.dup(0), os.dup(1)  # not inheritable (PEP 446)
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    try:
        os.dup2(2, 1)
    except OSError:  # started without stderr
        os.dup2(null, 1)
    os.close(null)
    if sys.platform == "win32":  # what new processes get as their standard handles
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))  # STD_INPUT_HANDLE
        kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(1))  # STD_OUTPUT_HANDLE
    return protocol_in, protocol_out


def serve_stdio(*, cache_dir: str | None = None, workers: int | None = None) -> None:
    """Run the engine service on this process's stdin/stdout until shutdown or EOF."""
    protocol_in, protocol_out = _claim_stdio()
    stdin = open(protocol_in, encoding="utf-8")  # noqa: SIM115 - process lifetime
    stdout = open(protocol_out, "w", encoding="utf-8", newline="\n")  # noqa: SIM115
    server = JsonRpcServer(stdin, stdout)
    service = EngineService(server, cache_dir=cache_dir, workers=workers)
    try:
        server.serve_forever()
    finally:
        service.close()
