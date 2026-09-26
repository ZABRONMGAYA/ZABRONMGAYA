"""The engine service: every JSON-RPC method the desktop app calls.

Method names and payloads are the IPC contract of docs/ARCHITECTURE.md §6.
Clip, device and correction ids are the project database's integer ids; the
sync engine sees clip ids as their decimal strings.

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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from mcsync import __version__
from mcsync.export import ExportOptions, export_timeline
from mcsync.media.cache import AnalysisCache
from mcsync.media.extract import extract_to_cache
from mcsync.media.library import build_clip_inputs, scan_media
from mcsync.media.tools import FFmpegNotFound, find_tools
from mcsync.media.waveform import PEAK_LEVELS
from mcsync.project.db import ClipRow, Project, ProjectError
from mcsync.serialize import to_jsonable
from mcsync.sync.engine import CandidatePair, SyncEngine, SyncOptions, create_match_pool
from mcsync.sync.params import DEFAULT_PARAMS, SyncParams
from mcsync.sync.types import ClipInput, PairwiseMatch, SyncMode
from mcsync.timecode import parse_frame_rate
from mcsync.timeline import build_timeline

from .jobs import Job, JobManager
from .rpc import APP_ERROR, BUSY, FFMPEG_MISSING, NO_PROJECT, JsonRpcServer, RpcError

PROTOCOL_VERSION = 1
MATCH_KEY_VERSION = "2"
PARALLEL_MIN_PAIRS = 8
SETTINGS_DEFAULTS: dict[str, Any] = {
    "mode": SyncMode.HYBRID.value,
    "reference_clip_id": None,
    "timecode_jam_synced": False,
    "use_creation_time": True,
}


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
        self.workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
        self.params: SyncParams = DEFAULT_PARAMS
        self.project: Project | None = None
        self.jobs = JobManager(self.notify, log=lambda text: print(text, file=sys.stderr))
        self._pool: ProcessPoolExecutor | None = None
        if server is None:
            return
        server.error_mappers.append(self._map_error)
        for name in (
            "engine.hello", "engine.shutdown",
            "project.create", "project.open", "project.close", "project.info", "project.update_settings",
            "media.import", "media.list", "media.remove",
            "device.update", "clip.assign_device", "clip.set_audio",
            "sync.run", "sync.solve", "sync.snap", "sync.matches",
            "correction.add", "correction.undo", "correction.redo", "correction.list",
            "timeline.get", "waveform.info", "export.xml",
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

    def _require_project(self) -> Project:
        if self.project is None:
            raise RpcError(NO_PROJECT, "no project is open")
        return self.project

    def _require_idle(self, kinds: set[str]) -> None:
        busy = self.jobs.running(kinds)
        if busy:
            raise RpcError(BUSY, f"a {busy[0].kind} job is running", {"job_id": busy[0].id})

    def close(self) -> None:
        self.jobs.cancel_all()
        for job in list(self.jobs.jobs.values()):
            job.finished.wait(10)
        if self._pool is not None:
            self._pool.shutdown(cancel_futures=True)
            self._pool = None
        if self.project is not None:
            self.project.close()
            self.project = None

    # ---------------------------------------------------------------- engine

    def engine_hello(self, client: str | None = None) -> dict:
        try:
            ffmpeg = find_tools().version()
        except FFmpegNotFound:
            ffmpeg = None
        return {
            "name": "mcsync-engine",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "ffmpeg": ffmpeg,
            "cache_dir": str(self.cache.root),
            "workers": self.workers,
        }

    def engine_shutdown(self) -> dict:
        self.close()
        if self.server is not None:
            self.server.stop()
        return {"ok": True}

    # --------------------------------------------------------------- project

    def _project_summary(self) -> dict:
        project = self._require_project()
        clips = project.clips()
        return {
            "path": str(project.path),
            "name": project.name,
            "settings": {**SETTINGS_DEFAULTS, **project.settings()},
            "clips": len(clips),
            "offline": sum(c.status != "online" for c in clips),
            "devices": len(project.devices()),
            "last_run": project.last_completed_run(),
        }

    def project_create(self, path: str, name: str | None = None) -> dict:
        self._require_idle({"import", "sync"})
        if self.project is not None:
            self.project.close()
        self.project = Project.create(path, name)
        return self._project_summary()

    def project_open(self, path: str) -> dict:
        self._require_idle({"import", "sync"})
        if self.project is not None:
            self.project.close()
        self.project = Project.open(path)
        return self._project_summary()

    def project_close(self) -> dict:
        self._require_idle({"import", "sync"})
        if self.project is not None:
            self.project.close()
            self.project = None
        return {"ok": True}

    def project_info(self) -> dict:
        return self._project_summary()

    def project_update_settings(self, **settings: Any) -> dict:
        unknown = set(settings) - set(SETTINGS_DEFAULTS)
        if unknown:
            raise RpcError(APP_ERROR, f"unknown settings: {sorted(unknown)}")
        if "mode" in settings:
            SyncMode(settings["mode"])
        project = self._require_project()
        project.update_settings(**settings)
        return {**SETTINGS_DEFAULTS, **project.settings()}

    # ----------------------------------------------------------------- media

    def media_import(self, paths: list[str], recursive: bool = True) -> dict:
        project = self._require_project()
        self._require_idle({"import", "sync"})
        tools = find_tools()

        def work(job: Job) -> dict:
            items, problems = scan_media(
                paths, tools=tools, recursive=recursive, progress=lambda f, m: job.report(0.2 * f, m)
            )
            job.check_cancelled()
            clip_ids = project.add_media(items)
            for k, (clip_id, item) in enumerate(zip(clip_ids, items, strict=True)):
                job.check_cancelled()
                if item.audio_stream is not None:
                    entry = self.cache.entry(
                        item.fingerprint, item.audio_stream.index, channel=item.channel, params=self.params
                    )
                    extract_to_cache(
                        item.info, entry, stream=item.audio_stream, params=self.params, tools=tools, cancel=job.cancel
                    )
                job.report(0.2 + 0.8 * (k + 1) / max(len(items), 1), f"Extracted {Path(item.path).name}")
                self.notify("media.imported", {"clip_id": clip_id, "path": item.path})
            return {"clip_ids": clip_ids, "problems": [to_jsonable(p) for p in problems]}

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
        }

    def media_list(self) -> dict:
        project = self._require_project()
        return {"clips": [self._clip_summary(r) for r in project.clips()], "devices": project.devices()}

    def media_remove(self, clip_ids: list[int]) -> dict:
        self._require_idle({"import", "sync"})
        self._require_project().remove_clips(clip_ids)
        return self.media_list()

    def device_update(self, device_id: int, name: str | None = None, kind: str | None = None) -> dict:
        self._require_project().update_device(device_id, name=name, kind=kind)
        return self.media_list()

    def clip_assign_device(self, clip_id: int, device_id: int) -> dict:
        self._require_project().set_clip_device(clip_id, device_id)
        return self.media_list()

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
        return self._clip_summary(project.clip(clip_id))

    # ------------------------------------------------------------------ sync

    def _settings(self) -> dict:
        return {**SETTINGS_DEFAULTS, **self._require_project().settings()}

    def _clip_inputs(self, rows: list[ClipRow], job: Job | None = None) -> list[ClipInput]:
        """Engine inputs for the project's clips, extracting any audio not yet cached."""
        settings = self._settings()
        items = []
        for k, row in enumerate(rows):
            item = row.to_media_item()
            if item.audio_stream is not None and row.status != "offline":
                entry = self.cache.entry(row.fingerprint, item.audio_stream.index, channel=row.audio_channel,
                                         params=self.params)  # fmt: skip
                if not entry.exists():
                    if job is not None:
                        job.report(0.1 * k / len(rows), f"Extracting audio from {row.name}")
                    extract_to_cache(row.info, entry, stream=item.audio_stream, channel=row.audio_channel,
                                     params=self.params, cancel=job.cancel if job else None)  # fmt: skip
                item.signal = entry.load()
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

    def _pair_key(self, pair: CandidatePair, rows: dict[str, ClipRow]) -> str:
        def side(clip: ClipInput) -> list:
            row = rows[clip.clip_id]
            return [row.fingerprint, row.audio_stream, row.audio_channel, round(clip.audio_start_s, 6)]

        window = None if pair.window is None else [round(pair.window[0], 3), round(pair.window[1], 3)]
        text = json.dumps([MATCH_KEY_VERSION, repr(self.params), side(pair.ref), side(pair.tgt), window])
        return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()

    def _pool_for(self, n_pairs: int) -> ProcessPoolExecutor | None:
        if self.workers <= 1 or n_pairs < PARALLEL_MIN_PAIRS:
            return None
        if self._pool is None:
            self._pool = create_match_pool(self.workers)
        return self._pool

    def sync_run(
        self, mode: str | None = None, reference_clip_id: int | None = None, timecode_jam_synced: bool | None = None
    ) -> dict:
        project = self._require_project()
        self._require_idle({"import", "sync"})
        changes = {"mode": mode, "reference_clip_id": reference_clip_id, "timecode_jam_synced": timecode_jam_synced}
        self.project_update_settings(**{k: v for k, v in changes.items() if v is not None})

        def work(job: Job) -> dict:
            rows = project.clips()
            by_engine_id = {r.engine_id: r for r in rows}
            clips = self._clip_inputs(rows, job)
            engine = self._engine()
            corrections = project.corrections()
            active = [c for c in clips if c.clip_id not in corrections.excluded_clips]
            pairs = engine.candidate_pairs(active) if engine.options.mode != SyncMode.TIMECODE else []
            keys = [self._pair_key(p, by_engine_id) for p in pairs]
            known = project.find_matches(keys)
            run_id = project.start_run({"settings": self._settings(), "params": repr(self.params)})
            key_of: dict[tuple[str, str], str] = {}
            todo = []
            for pair, key in zip(pairs, keys, strict=True):
                key_of[(pair.ref.clip_id, pair.tgt.clip_id)] = key
                if key in known:  # content unchanged since it was matched: reuse, under today's clip ids
                    project.save_match(run_id, key, replace(known[key], ref_id=pair.ref.clip_id,
                                                            tgt_id=pair.tgt.clip_id))  # fmt: skip
                else:
                    todo.append(pair)

            def save(match: PairwiseMatch) -> None:
                project.save_match(run_id, key_of[(match.ref_id, match.tgt_id)], match)

            try:
                engine.match_pairs(
                    todo,
                    pool=self._pool_for(len(todo)),
                    cancel=job.cancel,
                    progress=lambda f, m: job.report(0.1 + 0.85 * f, m),
                    on_match=save,
                )
            except BaseException:
                project.finish_run(run_id, "cancelled" if job.cancel.is_set() else "failed")
                raise
            project.finish_run(run_id, "completed")
            timeline = self._solve(clips)
            return {
                "run_id": run_id,
                "pairs": len(pairs),
                "reused": len(pairs) - len(todo),
                "matched": len(todo),
                "timeline": timeline,
            }

        return {"job_id": self.jobs.start("sync", work).id}

    def _solve(self, clips: list[ClipInput] | None = None) -> dict:
        project = self._require_project()
        rows = project.clips()
        if not rows:
            return to_jsonable(build_timeline([], {}))
        if clips is None:
            clips = self._clip_inputs(rows)
        run_id = project.last_completed_run()
        matches = project.run_matches(run_id) if run_id is not None else []
        current = {c.clip_id for c in clips}
        matches = [m for m in matches if m.ref_id in current and m.tgt_id in current]
        result = self._engine().solve(clips, matches, project.corrections())
        project.save_placements(result)
        placements = {int(cid): p for cid, p in result.placements.items()}
        return to_jsonable(build_timeline(rows, placements, int(result.reference_id)))

    def sync_solve(self) -> dict:
        self._require_idle({"sync"})
        return self._solve()

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
        for m in project.run_matches(run_id):
            if me not in (m.ref_id, m.tgt_id) or m.status.value == "no_match":
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
