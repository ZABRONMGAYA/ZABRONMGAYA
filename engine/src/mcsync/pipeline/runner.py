"""The project pipeline: every file from discovery to a synchronised timeline, in the background.

    discovery → metadata → database → audio analysis (analysis signal, waveform, fingerprint)
      → candidates (clocks, fingerprint votes) → verification → extended search → solve → results

Every unit of work is a row of the project's task table: ``probe`` per file, ``analyze`` per clip, ``match`` and
``extend`` per pair of clips. The queue therefore survives pauses, crashes and restarts, and the interface can
count, cancel, retry and reprioritise it. One controller thread claims tasks in priority order and hands them to
three worker pools sized for the machine (:mod:`mcsync.resources`):

* ``probe``: threads (ffprobe processes and 2 MiB reads; the disk is the limit);
* ``analyze``: threads (each drives one FFmpeg decoder and streams its output through the band filter, waveform
  and fingerprint code, which runs in NumPy and SciPy outside the interpreter lock);
* ``match``: processes (the matcher is processor-bound Python), shared with the service.

Results are written back in batches (a transaction per stage every fraction of a second), never one per file, and
no stage ever holds more than one file's audio in memory per worker.

A sync run moves through phases stored in the project (``meta['sync']``), so a run interrupted by a crash resumes
where it stopped: ``waiting`` (for metadata and audio analysis) → ``planning`` (candidate pairs) → ``matching`` →
``extending`` (extended search for clips still unmatched) → ``solving`` → done.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from mcsync.ai import speakers as voices
from mcsync.ai.speech import TranscriptionCancelled, chunks
from mcsync.media.devices import find_chapters, identify_device
from mcsync.media.extract import ExtractionCancelled, extract_to_cache
from mcsync.media.fingerprint import fingerprint
from mcsync.media.library import MediaItem
from mcsync.media.probe import ProbeError, probe
from mcsync.project.db import Project, Task
from mcsync.sync.candidates import (
    EXHAUSTIVE_PAIR_BUDGET,
    PlannedPair,
    clock_pairs,
    cross_device_pairs,
    exhaustive_pairs,
    fallback_partners,
    landmark_pairs,
    merge_plans,
)
from mcsync.sync.engine import verify_pair
from mcsync.sync.landmarks import IndexClip, LandmarkIndex, ensure_landmarks, landmark_file, query_many
from mcsync.sync.types import ClipInput, MatchStatus, SyncMode

from .discovery import Found, WalkProblem, walk

if TYPE_CHECKING:
    from mcsync.resources import WorkerPlan
    from mcsync.service.app import EngineService

PRIORITY = {"high": 1, "normal": 5, "low": 8}
PROGRESS_INTERVAL_S = 0.25
_FLUSH_INTERVAL_S = 0.3
_QUERY_BATCH = 16
MAX_ATTEMPTS = 3
PHASES = ("waiting", "planning", "matching", "extending", "solving", "done")


class PipelineStopped(Exception):
    pass


@dataclass
class _Running:
    task: Task
    pool: str
    name: str
    started: float
    cancel: threading.Event | None = None
    discard: bool = False


@dataclass
class ImportRequest:
    id: int
    paths: list[str]
    priority: int
    walking: bool = True
    found: int = 0
    discovered_ids: list[int] = field(default_factory=list)
    problems: list[WalkProblem] = field(default_factory=list)


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Pipeline:
    """Background processing of one open project (see the module docstring)."""

    def __init__(self, service: EngineService, project: Project, plan: WorkerPlan, *, start_paused: bool = False):
        self.service = service
        self.project = project
        self.plan = plan
        self._cond = threading.Condition()
        self._paused = start_paused
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._running: dict[Future, _Running] = {}
        self._finished: list[tuple[_Running, Any, BaseException | None]] = []
        self._last_flush = 0.0
        self._aux: Future | None = None
        self._aux_label = ""
        self._aux_pool = ThreadPoolExecutor(1, thread_name_prefix="pipeline-aux")
        self._walk_pool = ThreadPoolExecutor(1, thread_name_prefix="pipeline-walk")
        self._probe_pool = ThreadPoolExecutor(max(1, plan.probe), thread_name_prefix="probe")
        self._analyze_pool = ThreadPoolExecutor(max(1, plan.analyze), thread_name_prefix="analyze")
        # Transcription: one task at a time; the speech models use several threads themselves.
        self._speech_pool = ThreadPoolExecutor(1, thread_name_prefix="speech")
        self._speakers: list[voices.Speaker] | None = None
        self._maybe_pending = {"probe": True, "analyze": True, "match": True, "transcribe": True}
        self._activity: deque[dict] = deque(maxlen=300)
        self._done_times: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=5000))
        self._last_progress = 0.0
        self._chapters_dirty = False
        self._inputs: dict[str, ClipInput] | None = None
        self._inputs_version = -1
        self._weak: dict[int, dict] = {}
        self._imports: dict[int, ImportRequest] = {}
        self._next_import = 1
        self._listeners: list[Callable[[str, dict], None]] = []
        self._sync: dict | None = project.meta_get("sync")
        self._planned: dict[int, dict] = {}
        self._error: str | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._cond:
            if self._thread is None or not self._thread.is_alive():
                self._stopping = False
                self._thread = threading.Thread(target=self._loop, name="pipeline", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def pause(self) -> None:
        """Stop starting work; interrupt running audio extraction (it returns to the queue)."""
        with self._cond:
            self._paused = True
            for r in self._running.values():
                if r.cancel is not None:
                    r.cancel.set()
            self._cond.notify_all()
        self._log("Paused", "")
        self._progress(force=True)

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._maybe_pending = dict.fromkeys(self._maybe_pending, True)
            self._cond.notify_all()
        self.start()
        self._log("Resumed", "")
        self._progress(force=True)

    @property
    def paused(self) -> bool:
        return self._paused

    def stop(self, timeout: float = 30.0) -> None:
        """Finish or interrupt running work, write every result, and end the controller (project closing)."""
        with self._cond:
            self._stopping = True
            for r in self._running.values():
                if r.cancel is not None:
                    r.cancel.set()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
        for pool in (self._probe_pool, self._analyze_pool, self._speech_pool, self._aux_pool, self._walk_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        # Anything still marked running goes back to the queue on the next open (Project.open resets it).

    def set_plan(self, plan: WorkerPlan) -> None:
        """Change worker counts; new pools take over, running work finishes in the old ones."""
        with self._cond:
            old = (self._probe_pool, self._analyze_pool)
            if plan.probe != self.plan.probe:
                self._probe_pool = ThreadPoolExecutor(max(1, plan.probe), thread_name_prefix="probe")
            if plan.analyze != self.plan.analyze:
                self._analyze_pool = ThreadPoolExecutor(max(1, plan.analyze), thread_name_prefix="analyze")
            self.plan = plan
            self._cond.notify_all()
        for pool in old:
            if pool is not self._probe_pool and pool is not self._analyze_pool:
                pool.shutdown(wait=False)
        self._progress(force=True)

    def add_listener(self, fn: Callable[[str, dict], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[str, dict], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, event: str, data: dict) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, data)
            except Exception:  # noqa: BLE001 - a listener must not stop the pipeline
                self.service.log(traceback.format_exc())
        self.service.notify(f"pipeline.{event}", data)

    def _wake(self, *kinds: str) -> None:
        with self._cond:
            for k in kinds:
                self._maybe_pending[k] = True
            self._cond.notify_all()

    def _log(self, what: str, text: str, level: str = "info") -> None:
        self._activity.append({"time": _now_hms(), "what": what, "text": text, "level": level})

    # ------------------------------------------------------------- import

    def import_paths(self, paths: list[str], *, recursive: bool = True, priority: int = PRIORITY["normal"]) -> int:
        """Discover media under ``paths`` in the background; every file found is queued for metadata."""
        with self._cond:
            request = ImportRequest(self._next_import, list(paths), priority)
            self._imports[request.id] = request
            self._next_import += 1
        self._walk_pool.submit(self._walk, request, recursive)
        self.start()
        return request.id

    def import_request(self, request_id: int) -> ImportRequest:
        return self._imports[request_id]

    def _walk(self, request: ImportRequest, recursive: bool) -> None:
        try:
            project = self.project
            roots: dict[str, int] = {}
            for p in request.paths:
                roots[p] = project.add_import_root(str(Path(p)))
            for batch in walk(request.paths, recursive=recursive, problems=request.problems,
                              should_stop=lambda: self._stopping):  # fmt: skip
                self._queue_found(batch, roots, request)
            self._log("Discovery", f"{request.found} files found")
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            request.problems.append(WalkProblem(",".join(request.paths), f"{type(exc).__name__}: {exc}"))
            self.service.log(traceback.format_exc())
        finally:
            request.walking = False
            self._wake("probe")
            self._progress(force=True)

    def _queue_found(self, batch: list[Found], roots: dict[str, int], request: ImportRequest | None) -> int:
        def root_of(path: str) -> int | None:
            for r, rid in roots.items():
                if path == r or path.startswith(r.rstrip("/\\") + ("\\" if "\\" in r else "/")):
                    return rid
            return None

        rows = [(f.path, f.size, f.mtime_ns, f.kind, root_of(f.path)) for f in batch]
        new_ids = self.project.add_discovered(rows)
        priority = request.priority if request else PRIORITY["normal"]
        known = self.project.discovered(new_ids)
        self.project.enqueue(
            "probe",
            [{"target": i, "priority": priority, "detail": {"path": known[i]["path"]}} for i in new_ids],
        )
        if request is not None:
            request.found += len(batch)
            request.discovered_ids += new_ids
        self._wake("probe")
        self._progress()
        return len(new_ids)

    def rescan(self) -> dict:
        """Walk every import root again: queue new files, re-read changed ones, mark missing ones offline."""
        roots = self.project.import_roots()
        known = self.project.known_paths()
        changed: list[Found] = []
        new = 0
        root_ids = {r["path"]: r["id"] for r in roots}
        present = [r["path"] for r in roots if Path(r["path"]).exists()]
        for batch in walk(present):
            fresh = [f for f in batch if f.path not in known]
            changed += [f for f in batch if f.path in known and known[f.path] != (f.size, f.mtime_ns)]
            if fresh:
                new += self._queue_found(fresh, root_ids, None)
        if changed:
            ids = self.project.rediscover([(f.path, f.size, f.mtime_ns) for f in changed])
            self.project.enqueue(
                "probe", [{"target": i, "detail": {"path": p}} for i, p in ids], requeue=True
            )  # fmt: skip
            self._wake("probe")
        status = self.project.refresh_media_status()
        self.start()
        return {"new": new, "changed": len(changed), "offline": status.get("offline", 0), "roots": len(roots)}

    # ------------------------------------------------------------- queue control

    def retry(self, **kw: Any) -> int:
        n = self.project.retry_tasks(**kw)
        self._wake("probe", "analyze", "match")
        self.start()
        return n

    def cancel(self, *, ids: list[int] | None = None, clip_ids: list[int] | None = None,
               kinds: list[str] | None = None, running: bool = True) -> int:  # fmt: skip
        """Cancel pending tasks (all, or those selected) and, with ``running``, the matching running ones."""
        n = self.project.cancel_tasks(ids=ids, clip_ids=clip_ids, kinds=kinds)
        if running:
            with self._cond:
                for r in self._running.values():
                    t = r.task
                    hit = (
                        (ids is None or t.id in ids)
                        and (kinds is None or t.kind in kinds)
                        and (clip_ids is None or t.clip_id in clip_ids or t.other_clip_id in clip_ids)
                    )
                    if hit:
                        r.discard = True
                        if r.cancel is not None:
                            r.cancel.set()
                        n += 1
        self._progress(force=True)
        return n

    def prioritize(self, clip_ids: list[int], priority: int) -> int:
        n = self.project.prioritize(clip_ids, priority)
        self._wake("probe", "analyze", "match")
        return n

    def analyze(self, clip_ids: list[int] | None = None, *, requeue: bool = False, priority: int = 5) -> int:
        """Queue audio analysis for these clips (default: every clip with audio that has none)."""
        rows = self.project.clips() if clip_ids is None else [self.project.clip(i) for i in clip_ids]
        done = self.project.audio_analysis()
        items = [
            {"target": r.id, "clip_id": r.id, "priority": priority}
            for r in rows
            if r.audio_stream is not None and (requeue or clip_ids is not None or r.id not in done)
        ]
        n = self.project.enqueue("analyze", items, requeue=requeue or clip_ids is not None)
        self._wake("analyze")
        self.start()
        return n

    # ------------------------------------------------------------- sync runs

    @property
    def sync_state(self) -> dict | None:
        return self._sync

    def start_sync(self, params: dict) -> int:
        """Start a sync run over every clip (it waits for metadata and audio analysis still in progress)."""
        old = self._sync
        if old is not None and old.get("phase") != "done":
            self._abandon_run(old["run_id"], "cancelled")
        run_id = self.project.start_run(params)
        self._set_sync({"run_id": run_id, "phase": "waiting", "started": time.time(), "planned": 0, "reused": 0})
        self._log("Sync", f"run {run_id} started")
        self.analyze()  # clips never analysed (e.g. analysis cancelled earlier)
        self.resume()
        return run_id

    def cancel_sync(self) -> bool:
        s = self._sync
        if s is None or s.get("phase") == "done":
            return False
        self._abandon_run(s["run_id"], "cancelled")
        self._set_sync(None)
        self._progress(force=True)
        return True

    def _abandon_run(self, run_id: int, status: str) -> None:
        self.project.cancel_tasks(kinds=["match", "extend"])
        with self._cond:
            for r in self._running.values():
                if r.task.kind in ("match", "extend"):
                    r.discard = True
        self.project.finish_run(run_id, status)
        self._emit("sync_finished", {"run_id": run_id, "status": status})

    def _set_sync(self, state: dict | None) -> None:
        self._sync = state
        self.project.meta_set("sync", state)
        self._progress(force=True)

    # ------------------------------------------------------------- controller

    def _loop(self) -> None:
        try:
            while True:
                with self._cond:
                    if self._stopping and not self._running:
                        break
                    active = not (self._paused or self._stopping)
                started = self._fill() if active else 0
                self._collect(0.2 if self._running else 0.0)
                self._apply(force=self._stopping)
                if active:
                    self._advance()
                self._progress()
                with self._cond:
                    idle = (
                        not self._running
                        and not started
                        and not self._finished
                        and (self._aux is None or self._aux.done())
                    )
                    if idle and not self._stopping:
                        self._cond.wait(timeout=1.0 if active else 5.0)
            self._apply(force=True)
        except Exception as exc:  # noqa: BLE001 - reported to the app
            self._error = f"{type(exc).__name__}: {exc}"
            self.service.log(traceback.format_exc())
            self._emit("error", {"error": self._error})
        finally:
            self._progress(force=True)

    def _count_running(self, pool: str) -> int:
        return sum(1 for r in self._running.values() if r.pool == pool)

    def _fill(self) -> int:
        started = 0
        for kind, pool, limit in (
            ("probe", "probe", self.plan.probe),
            ("analyze", "analyze", self.plan.analyze),
            ("match", "match", self.service.match_workers),
            ("transcribe", "speech", 1),
        ):
            if not self._maybe_pending.get(kind):
                continue
            free = limit - self._count_running(pool)
            if free <= 0:
                continue
            kinds = ["match", "extend"] if kind == "match" else [kind]
            if kind == "match" and (self._sync is None or self._sync.get("phase") not in ("matching", "extending")):
                continue
            # Checked again under the lock at the moment of claiming: the loop decided to fill before a pause may
            # have arrived, and nothing may start once pause() has returned.
            with self._cond:
                if self._paused or self._stopping:
                    return started
                tasks = self.project.claim(kinds, free)
            if len(tasks) < free:
                self._maybe_pending[kind] = False
            for t in tasks:
                self._submit(t)
                started += 1
        return started

    def _submit(self, t: Task) -> None:
        if t.kind == "probe":
            path = t.detail.get("path", "")
            run = _Running(t, "probe", Path(path).name, time.monotonic())
            fut = self._probe_pool.submit(self._do_probe, path)
        elif t.kind == "analyze":
            run = _Running(t, "analyze", "", time.monotonic(), threading.Event())
            fut = self._analyze_pool.submit(self._do_analyze, t.clip_id, run.cancel)
        elif t.kind == "transcribe":
            run = _Running(t, "speech", "", time.monotonic(), threading.Event())
            fut = self._speech_pool.submit(self._do_transcribe, t, run.cancel)
        else:
            inputs = self._clip_inputs()
            ref, tgt = inputs.get(str(t.clip_id)), inputs.get(str(t.other_clip_id))
            run = _Running(t, "match", "", time.monotonic())
            if ref is None or tgt is None or ref.audio is None or tgt.audio is None:
                fut = Future()
                fut.set_exception(_Skip("clip removed or without analysed audio"))
            else:
                run.name = f"{self._name(t.clip_id)} ↔ {self._name(t.other_clip_id)}"
                windows = [tuple(w) if w is not None else None for w in t.detail.get("windows", [None])]
                try:
                    fut = self.service.match_pool().submit(
                        verify_pair, self.service._engine().options, ref, tgt, windows, t.detail.get("stage", "full")
                    )
                except BrokenProcessPool:
                    self.service.reset_match_pool()
                    fut = self.service.match_pool().submit(
                        verify_pair, self.service._engine().options, ref, tgt, windows, t.detail.get("stage", "full")
                    )
        with self._cond:
            self._running[fut] = run

    def _name(self, clip_id: int | None) -> str:
        if clip_id is None:
            return ""
        inputs = self._names()
        return inputs.get(clip_id, str(clip_id))

    def _names(self) -> dict[int, str]:
        version = self.project.clip_version
        cached = getattr(self, "_names_cache", None)
        if cached is None or cached[0] != version:
            cached = (version, {r.id: r.name for r in self.project.clips()})
            self._names_cache = cached
        return cached[1]

    def _collect(self, timeout: float) -> None:
        with self._cond:
            futures = list(self._running)
        if not futures:
            return
        done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
        with self._cond:
            for fut in done:
                run = self._running.pop(fut)
                exc = fut.exception()
                self._finished.append((run, None if exc else fut.result(), exc))

    # ------------------------------------------------------------- work (threads)

    def _do_probe(self, path: str) -> MediaItem:
        tools = self.service.tools()
        info = probe(path, tools)
        return MediaItem(info=info, fingerprint=fingerprint(path), device=identify_device(info),
                         audio_stream=info.primary_audio)  # fmt: skip

    def _do_analyze(self, clip_id: int, cancel: threading.Event) -> dict:
        row = self.project.clip(clip_id)
        stream = row.audio_stream_info
        if stream is None:
            raise _Skip("no audio stream")
        entry = self.service.cache.entry(row.fingerprint, stream.index, channel=row.audio_channel,
                                         params=self.service.params)  # fmt: skip
        if not entry.exists() and row.status == "offline":
            raise _Skip("media offline")
        signal = extract_to_cache(row.info, entry, stream=stream, channel=row.audio_channel,
                                  params=self.service.params, tools=self.service.tools(), cancel=cancel)  # fmt: skip
        lm = ensure_landmarks(entry.directory, signal.samples, signal.rate, self.service.params)
        n_hashes = max(0, (lm.stat().st_size - 128) // 8)
        return {
            "clip_id": clip_id,
            "cache_key": str(entry.directory),
            "rate": signal.rate,
            "samples": signal.n_samples,
            "level_dbfs": signal.level_dbfs if signal.level_dbfs != float("-inf") else None,
            "n_hashes": n_hashes,
            "status": "done",
            "name": row.name,
        }

    def _do_transcribe(self, t: Task, cancel: threading.Event) -> dict:
        row = self.project.clip(t.clip_id)  # type: ignore[arg-type]
        stream = row.audio_stream_info
        if stream is None:
            raise _Skip("no audio stream")
        if row.status == "offline":
            raise _Skip("media offline")
        d = t.detail
        transcriber = self.service.transcriber(d["model"], d["language"])
        utterances, events = transcriber.transcribe_range(
            self.service.tools(), row.path, stream.index, row.audio_channel, stream.channels,
            float(d["start_s"]), float(d["end_s"]), cancel,
        )  # fmt: skip
        return {"clip_id": row.id, "chunk": int(d["chunk"]), "span": (float(d["start_s"]), float(d["end_s"])),
                "utterances": utterances, "events": events, "name": row.name}  # fmt: skip

    # ------------------------------------------------------------- transcription

    def transcribe(self, clip_ids: list[int], model: str, language: str, *, priority: int = PRIORITY["normal"],
                   redo: bool = False) -> int:  # fmt: skip
        """Queue transcription of these clips, one task per chunk. Returns how many tasks became pending."""
        rows = {r.id: r for r in self.project.clips()}
        items = []
        for clip_id in clip_ids:
            row = rows.get(clip_id)
            if row is None or row.audio_stream is None:
                continue
            spans = chunks(row.info.duration_s)
            self.project.set_transcript_state(clip_id, model, language, len(spans))
            items += [
                {"target": f"{clip_id}:{k}", "clip_id": clip_id, "priority": priority,
                 "detail": {"chunk": k, "start_s": a, "end_s": b, "model": model, "language": language}}
                for k, (a, b) in enumerate(spans)
            ]  # fmt: skip
        if redo:
            self.project.clear_transcripts(clip_ids)
        n = self.project.enqueue("transcribe", items, requeue=True)
        with self._cond:
            self._maybe_pending["transcribe"] = True
            self._cond.notify_all()
        self._log("Transcription", f"{len({i['clip_id'] for i in items})} clip(s) queued")
        return n

    def forget_speakers(self) -> None:
        """The user renamed or merged speakers: reload them before assigning new utterances."""
        with self._cond:
            self._speakers = None

    def _speaker_list(self) -> list[voices.Speaker]:
        if self._speakers is None:
            self._speakers = [
                voices.Speaker(r["key"], np.frombuffer(r["centroid"], dtype=np.float32).copy(), int(r["utterances"]))
                for r in self.project.speakers()
            ]
        return self._speakers

    # ------------------------------------------------------------- results (controller thread)

    def _apply(self, force: bool = False) -> None:
        with self._cond:
            if not self._finished or (not force and time.monotonic() - self._last_flush < _FLUSH_INTERVAL_S
                                      and len(self._finished) < 64):  # fmt: skip
                return
            finished, self._finished = self._finished, []
        self._last_flush = time.monotonic()
        by_pool: dict[str, list] = defaultdict(list)
        for item in finished:
            by_pool[item[0].pool].append(item)
        if by_pool["probe"]:
            self._apply_probes(by_pool["probe"])
        if by_pool["analyze"]:
            self._apply_analyses(by_pool["analyze"])
        if by_pool["match"]:
            self._apply_matches(by_pool["match"])
        if by_pool["speech"]:
            self._apply_transcripts(by_pool["speech"])

    def _finish(self, results: list[tuple[int, str, str | None]], release: list[int]) -> None:
        if release:
            self.project.release_tasks(release)
        if results:
            self.project.finish_tasks(results)

    def _apply_probes(self, items: list) -> None:
        good: list[tuple[_Running, MediaItem]] = []
        results: list[tuple[int, str, str | None]] = []
        discovered: list[tuple[int, str, int | None, str | None]] = []
        release: list[int] = []
        for run, value, exc in items:
            t = run.task
            if exc is None:
                good.append((run, value))
                continue
            if run.discard:
                continue
            if isinstance(exc, ProbeError):
                # Not footage (a stray document, a RAW format FFmpeg cannot read) is left out, not reported as an
                # error; a media file that cannot be read is an error.
                status, message = ("skipped" if exc.skip else "failed"), exc.reason
            elif isinstance(exc, OSError) and t.attempts < MAX_ATTEMPTS and not Path(t.detail.get("path", "")).exists():
                status, message = "failed", "file not found"
            else:
                status, message = "failed", _message(exc)
            results.append((t.id, status, message))
            discovered.append((int(t.target), status, None, message))
            self._log("Metadata", f"{run.name} · {message}", "error" if status == "failed" else "warn")
        if good:
            clip_ids = self.project.add_media([m for _, m in good])
            rows = {c.id: c for c in self.project.clips()}
            analyze = []
            for (run, item), clip_id in zip(good, clip_ids, strict=True):
                t = run.task
                results.append((t.id, "done", None))
                discovered.append((int(t.target), "done", rows[clip_id].media_id if clip_id in rows else None, None))
                if item.audio_stream is not None:
                    analyze.append({"target": clip_id, "clip_id": clip_id, "priority": t.priority})
                self.service.notify("media.imported", {"clip_id": clip_id, "path": item.path})
                self._done_times["probe"].append(time.monotonic())
            if analyze:
                self.project.enqueue("analyze", analyze, requeue=True)
                self._maybe_pending["analyze"] = True
            self._chapters_dirty = True
            self._log("Metadata", f"{len(good)} files read")
        self._finish(results, release)
        if discovered:
            self.project.finish_discovered(discovered)

    def _apply_analyses(self, items: list) -> None:
        results: list[tuple[int, str, str | None]] = []
        rows: list[dict] = []
        release: list[int] = []
        for run, value, exc in items:
            t = run.task
            if exc is None:
                results.append((t.id, "done", None))
                name = value.pop("name")
                rows.append(value)
                self._done_times["analyze"].append(time.monotonic())
                self._log("Audio", f"{name} analysed")
            elif isinstance(exc, ExtractionCancelled) and not run.discard:
                release.append(t.id)  # paused: back to the queue
            elif run.discard:
                continue
            elif isinstance(exc, _Skip):
                results.append((t.id, "skipped", str(exc)))
                rows.append({"clip_id": t.clip_id, "cache_key": "", "status": "skipped"})
            else:
                results.append((t.id, "failed", _message(exc)))
                rows.append({"clip_id": t.clip_id, "cache_key": "", "status": "failed"})
                self._log("Audio", f"{self._name(t.clip_id)} · {_message(exc)}", "error")
        if rows:
            self.project.set_audio_analysis(rows)
            self.service.notify("media.analyzed", {"clip_ids": [r["clip_id"] for r in rows]})
        self._finish(results, release)

    def _apply_transcripts(self, items: list) -> None:
        results: list[tuple[int, str, str | None]] = []
        release: list[int] = []
        touched: set[int] = set()
        known = self._speaker_list()
        for run, value, exc in items:
            t = run.task
            if exc is None:
                keys = voices.assign([u.fingerprint for u in value["utterances"]], known)
                rows = [
                    (u.start_s, u.end_s, key, u.language, u.text,
                     u.fingerprint.astype(np.float32).tobytes() if u.fingerprint is not None else None)
                    for u, key in zip(value["utterances"], keys, strict=True)
                ]  # fmt: skip
                events = [(e.t_s, e.label) for e in value["events"]]
                self.project.replace_transcript_chunk(value["clip_id"], value["chunk"], value["span"], rows, events)
                results.append((t.id, "done", None))
                touched.add(value["clip_id"])
                self._done_times["transcribe"].append(time.monotonic())
                self._log("Speech", f"{value['name']} · {len(rows)} utterance(s)")
            elif isinstance(exc, TranscriptionCancelled) and not run.discard:
                release.append(t.id)  # paused: back to the queue
            elif run.discard:
                continue
            elif isinstance(exc, _Skip):
                results.append((t.id, "skipped", str(exc)))
            else:
                results.append((t.id, "failed", _message(exc)))
                self._log("Speech", f"{self._name(t.clip_id)} · {_message(exc)}", "error")
        if touched:
            for absorbed, into in voices.merges(known):
                keep = next(s for s in known if s.key == into)
                self.project.merge_speakers(absorbed, into, keep.centroid.astype(np.float32).tobytes(), keep.count)
            self.project.save_speakers([(s.key, s.centroid.astype(np.float32).tobytes(), s.count) for s in known])
            self.service.notify("transcript.updated", {"clip_ids": sorted(touched)})
        self._finish(results, release)

    def _apply_matches(self, items: list) -> None:
        results: list[tuple[int, str, str | None]] = []
        release: list[int] = []
        saved = []
        sync = self._sync
        for run, value, exc in items:
            t = run.task
            if run.discard or sync is None or t.run_id != sync.get("run_id"):
                continue
            if exc is None:
                saved.append((t.target, value, t.detail.get("stage")))
                results.append((t.id, "done", None))
                self._done_times[t.kind].append(time.monotonic())
                if value.offset_s is not None and value.status != MatchStatus.NO_MATCH:
                    self._log("Match", f"{run.name} · {value.offset_s:+.3f} s · {value.confidence:.0%}")
            elif isinstance(exc, BrokenProcessPool):
                self.service.reset_match_pool()
                if t.attempts < MAX_ATTEMPTS:
                    release.append(t.id)
                else:
                    results.append((t.id, "failed", "the matcher process stopped (out of memory?)"))
            elif isinstance(exc, _Skip):
                results.append((t.id, "skipped", str(exc)))
            else:
                results.append((t.id, "failed", _message(exc)))
                self._log("Match", f"{run.name} · {_message(exc)}", "error")
        if saved and sync is not None:
            self.project.save_matches(sync["run_id"], saved)
            self._emit("matches", {"run_id": sync["run_id"], "count": len(saved)})
        self._finish(results, release)

    # ------------------------------------------------------------- stage transitions

    def _busy(self, *kinds: str) -> bool:
        if self.project.pending_count(list(kinds)) > 0:
            return True
        with self._cond:
            return any(r.task.kind in kinds for r in self._running.values()) or any(
                r.task.kind in kinds for r, _, _ in self._finished
            )

    def _advance(self) -> None:
        with self._cond:
            if self._paused or self._stopping:
                return
        if self._aux is not None:
            if not self._aux.done():
                return
            exc = self._aux.exception()
            label, self._aux = self._aux_label, None
            if exc is not None:
                self._log(label, _message(exc), "error")
                self.service.log("".join(traceback.format_exception(exc)))
                s = self._sync
                if s is not None:
                    self._abandon_run(s["run_id"], "failed")
                    self._set_sync(None)
                    self._emit("sync_finished", {"run_id": s["run_id"], "status": "failed", "error": _message(exc)})
                return
        walking = any(r.walking for r in self._imports.values())
        if self._chapters_dirty and not walking and not self._busy("probe"):
            self._chapters_dirty = False
            assign_chapters(self.project)
        s = self._sync
        if s is None:
            return
        phase = s.get("phase")
        if phase == "waiting":
            if not walking and not self._chapters_dirty and not self._busy("probe", "analyze"):
                self._set_sync({**s, "phase": "planning"})
                self._start_aux("Planning", self._plan_run, s["run_id"])
        elif phase == "planning":
            self._start_aux("Planning", self._plan_run, s["run_id"])
        elif phase == "matching":
            if not self._busy("match"):
                self._set_sync({**s, "phase": "extending"})
                self._start_aux("Extended search", self._plan_extended, s["run_id"])
        elif phase == "extending":
            if not self._busy("extend") and s.get("extended_planned"):
                self._set_sync({**s, "phase": "solving"})
                self._start_aux("Solving", self._solve_run, s["run_id"])
            elif not s.get("extended_planned"):
                self._start_aux("Extended search", self._plan_extended, s["run_id"])
        elif phase == "solving":
            self._start_aux("Solving", self._solve_run, s["run_id"])

    def _start_aux(self, label: str, fn: Callable, *args: Any) -> None:
        if self._aux is not None:
            return
        self._aux_label = label
        self._aux = self._aux_pool.submit(fn, *args)

    # ------------------------------------------------------------- inputs

    def _clip_inputs(self) -> dict[str, ClipInput]:
        """Engine inputs of every clip (signals map lazily), rebuilt only when clips change."""
        version = self.project.clip_version
        if self._inputs is None or self._inputs_version != version:
            rows = self.project.clips()
            self._inputs = {c.clip_id: c for c in self.service._clip_inputs(rows, extract=False)} if rows else {}
            self._inputs_version = version
        return self._inputs

    def _active(self) -> list[ClipInput]:
        corrections = self.project.corrections()
        ignored = {str(r.id) for r in self.project.clips() if r.ignored_duplicate}
        excluded = set(corrections.excluded_clips) | ignored
        return [c for c in self._clip_inputs().values() if c.clip_id not in excluded]

    # ------------------------------------------------------------- planning (aux thread)

    def _plan_run(self, run_id: int) -> None:
        project = self.project
        engine = self.service._engine()
        active = self._active()
        inputs = {c.clip_id: c for c in active}
        if engine.options.mode == SyncMode.TIMECODE:
            plans: list[PlannedPair] = []
            strategy = "timecode"
        elif cross_device_pairs(active) <= EXHAUSTIVE_PAIR_BUDGET:
            plans = exhaustive_pairs(engine, active)
            strategy = "exhaustive"
        else:
            strategy = "staged"
            found = self._landmark_candidates(run_id, active)
            plans = merge_plans(clock_pairs(engine, active), landmark_pairs(found, inputs))
        rows = {r.engine_id: r for r in project.clips()}
        keys = [self.service.pair_key(p, rows, inputs) for p in plans]
        known = project.known_pair_keys(keys)
        reuse = [(k, int(p.ref), int(p.tgt)) for p, k in zip(plans, keys, strict=True) if k in known]
        project.copy_matches(run_id, reuse)
        todo = [
            {"target": k, "clip_id": int(p.ref), "other_clip_id": int(p.tgt), "stage": p.stage, "run_id": run_id,
             "priority": 3, "detail": {"windows": p.windows, "stage": p.stage, "votes": p.votes}}
            for p, k in zip(plans, keys, strict=True) if k not in known
        ]  # fmt: skip
        project.enqueue("match", todo, requeue=True)
        self._planned[run_id] = {"pairs": len(plans), "reused": len(reuse), "strategy": strategy}
        s = self._sync or {}
        self._set_sync({**s, "phase": "matching", "planned": len(plans), "reused": len(reuse), "strategy": strategy})
        self._log("Candidates", f"{len(plans)} pairs to verify ({strategy}), {len(reuse)} already known")
        self._wake("match")

    def _landmark_candidates(self, run_id: int, active: list[ClipInput]) -> dict:
        analysis = self.project.audio_analysis()
        clips = []
        for c in active:
            a = analysis.get(int(c.clip_id))
            if c.audio is None or a is None or a["status"] != "done" or not a["cache_key"]:
                continue
            lm = landmark_file(Path(a["cache_key"]))
            if not lm.is_file():
                continue
            frames = max(0, 1 + (c.audio.n_samples - 512) // 256)
            clips.append(IndexClip(c.clip_id, str(lm), c.device_id, frames))
        root = self.service.cache.root / "index" / self.project.index_id
        index = LandmarkIndex.build(root, clips, self.service.params.analysis_rate)
        index.remove_others()
        self._log("Candidates", f"fingerprint index of {len(clips)} clips, {index.entries:,} hashes")
        found: dict[str, list] = {}
        keys = [c.key for c in clips]
        pool = self.service.match_pool()
        futures = [pool.submit(query_many, str(index.directory), keys[i : i + _QUERY_BATCH])
                   for i in range(0, len(keys), _QUERY_BATCH)]  # fmt: skip
        for fut in futures:
            if self._stopping:
                raise PipelineStopped()
            for key, cands in fut.result():
                found[key] = cands
            s = self._sync or {}
            self._sync = {**s, "queried": len(found), "to_query": len(keys)}
            self._progress()
        # Kept for the extended search: weak votes (below the candidate threshold) point it at likely partners.
        self._weak[run_id] = {"index": str(index.directory)}
        return found

    def _plan_extended(self, run_id: int) -> None:
        project = self.project
        engine = self.service._engine()
        active = self._active()
        inputs = {c.clip_id: c for c in active}
        summaries = project.run_match_summaries(run_id)
        confident: set[str] = set()
        tried: set[tuple[str, str]] = set()
        threshold = engine.options.solver.min_edge_confidence
        for m in summaries:
            a, b = str(m["ref_clip_id"]), str(m["tgt_clip_id"])
            tried.add((a, b) if a < b else (b, a))
            if m["status"] != "no_match" and (m["confidence"] or 0) >= max(threshold, 0.7):
                confident.update((a, b))
        placed_by_clock = self._clock_placed(active)
        unmatched = [c.clip_id for c in active if c.audio is not None and c.clip_id not in confident
                     and c.clip_id not in placed_by_clock]  # fmt: skip
        plans: list[PlannedPair] = []
        if unmatched and engine.options.mode != SyncMode.TIMECODE and len(inputs) > 1:
            weak = self._weak_votes(run_id, unmatched)
            times = {}
            for r in project.clips():
                if r.info.creation_time is not None:
                    times[r.engine_id] = r.info.creation_time.timestamp()
            plans = fallback_partners(unmatched, inputs, weak_votes=weak, creation_time=times, already=tried)
        rows = {r.engine_id: r for r in project.clips()}
        keys = [self.service.pair_key(p, rows, inputs) for p in plans]
        known = project.known_pair_keys(keys)
        reuse = [(k, int(p.ref), int(p.tgt)) for p, k in zip(plans, keys, strict=True) if k in known]
        project.copy_matches(run_id, reuse)
        project.enqueue("extend", [
            {"target": k, "clip_id": int(p.ref), "other_clip_id": int(p.tgt), "stage": p.stage, "run_id": run_id,
             "priority": 4, "detail": {"windows": p.windows, "stage": p.stage}}
            for p, k in zip(plans, keys, strict=True) if k not in known
        ], requeue=True)  # fmt: skip
        s = self._sync or {}
        self._set_sync({**s, "extended_planned": True, "unmatched": len(unmatched), "extended": len(plans)})
        self._log("Extended search", f"{len(unmatched)} clips without a confident match, {len(plans)} searches")
        self._wake("match")

    def _clock_placed(self, active: list[ClipInput]) -> set[str]:
        """Clips that shared clocks already place (chapters of a take, jam-synced timecode)."""
        by_domain: dict[str, set[str]] = defaultdict(set)
        for c in active:
            for clock in c.clocks:
                if not clock.domain.startswith("ct:"):
                    by_domain[clock.domain].add(c.clip_id)
        return {cid for members in by_domain.values() if len(members) > 1 for cid in members}

    def _weak_votes(self, run_id: int, unmatched: list[str]) -> dict:
        state = self._weak.get(run_id)
        if not state:
            return {}
        pool = self.service.match_pool()
        options = {"min_votes": 3, "z": 3.0, "max_clips": 8, "per_clip": 1}
        out: dict[str, list] = {}
        keys = [k for k in unmatched]
        futures = [pool.submit(query_many, state["index"], keys[i : i + _QUERY_BATCH], options)
                   for i in range(0, len(keys), _QUERY_BATCH)]  # fmt: skip
        for fut in futures:
            try:
                for key, cands in fut.result():
                    out[key] = cands
            except KeyError:  # a clip not in the index (no fingerprint)
                continue
        return out

    def _solve_run(self, run_id: int) -> None:
        project = self.project
        project.finish_run(run_id, "completed")
        timeline = self.service._solve()
        self._update_sessions()
        s = self._sync or {}
        elapsed = time.time() - s.get("started", time.time())
        self._set_sync({**s, "phase": "done", "finished": time.time(), "elapsed_s": elapsed})
        self._log("Sync", f"run {run_id} complete in {elapsed:.0f} s")
        self._emit("sync_finished", {"run_id": run_id, "status": "completed", "timeline": timeline})

    def _update_sessions(self) -> None:
        placements = self.project.placements()
        rows = self.project.clips()
        groups: dict[int, list] = defaultdict(list)
        evidence: set[int] = set()
        for r in rows:
            p = placements.get(r.id)
            if p is not None and p.group is not None:
                groups[p.group].append(r)
                if p.method.value in ("audio", "timecode", "chapter", "manual"):
                    evidence.add(p.group)
        sessions = []
        for g, members in groups.items():
            if g not in evidence or (g != 0 and len({r.device_id for r in members}) == 1):
                continue  # recording times alone, or one device's own clock: left unmatched for the editor
            times = [r.info.creation_time for r in members if r.info.creation_time is not None]
            start = min(times) if times else None
            ends = [r.info.creation_time.timestamp() + r.info.duration_s for r in members if r.info.creation_time]
            sessions.append({
                "group_no": g,
                "start_at": start.isoformat() if start else None,
                "end_at": datetime.fromtimestamp(max(ends), start.tzinfo).isoformat() if ends and start else None,
                "clip_ids": [r.id for r in members],
            })  # fmt: skip
        sessions.sort(key=lambda s: (s["start_at"] is None, s["start_at"] or "", s["group_no"]))
        for k, s in enumerate(sessions, 1):
            s["label"] = f"Session {k}"
        self.project.replace_auto_sessions(sessions)

    # ------------------------------------------------------------- progress

    def _rate(self, kind: str, window_s: float = 20.0) -> float | None:
        """Items per minute over the last ``window_s`` seconds (None when too few to say)."""
        times = self._done_times.get(kind)
        if not times:
            return None
        now = time.monotonic()
        recent = [t for t in times if now - t <= window_s]
        if len(recent) < 2:
            return None
        span = max(now - recent[0], 1e-3)
        return len(recent) / span * 60.0

    def snapshot(self) -> dict:
        project = self.project
        s = self._sync
        counts = project.task_counts(run_id=s["run_id"] if s else None)
        with self._cond:
            running = [
                {"kind": r.task.kind, "name": r.name or self._name(r.task.clip_id), "task_id": r.task.id,
                 "seconds": round(time.monotonic() - r.started, 1)}
                for r in self._running.values()
            ]  # fmt: skip
        empty = dict.fromkeys(("pending", "running", "done", "failed", "skipped", "cancelled"), 0)
        stages = {}
        for kind in ("probe", "analyze", "match", "extend", "transcribe"):
            c = {**empty, **counts.get(kind, {})}
            c["rate_per_min"] = self._rate(kind)
            stages[kind] = c
        walking = any(r.walking for r in self._imports.values())
        state = "paused" if self._paused else "running" if (running or walking or self._aux) else "idle"
        return {
            "state": state,
            "error": self._error,
            "discovery": {**project.discovery_counts(), "walking": walking},
            "stages": stages,
            "sync": dict(s) if s else None,
            "aux": self._aux_label if self._aux is not None else None,
            "running": running,
            "workers": {"probe": self.plan.probe, "analyze": self.plan.analyze, "match": self.service.match_workers},
            "activity": list(self._activity)[-60:],
        }

    def _progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        try:
            snap = self.snapshot()
        except Exception:  # noqa: BLE001 - the project may be closing
            return
        self._emit("progress", snap)


class _Skip(Exception):
    """Nothing to do for this task (reported as skipped, with the reason)."""


def _message(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[-1][:300]


def assign_chapters(project: Project) -> int:
    """Find takes split over several files (per device) and record each file's place in its take."""
    rows = project.clips()
    infos = [r.info for r in rows]
    keys = [r.device_key or f"clip:{r.id}" for r in rows]
    wanted: dict[int, tuple[str | None, int | None, float | None, int | None]] = {}
    for group in find_chapters(infos, keys):
        take_id = f"{keys[group[0]]}#{Path(infos[group[0]].path).name}"
        offset = 0.0
        for position, i in enumerate(group):
            wanted[rows[i].id] = (take_id, position, offset, rows[group[0]].device_id)
            offset += infos[i].duration_s
    updates = []
    for r in rows:
        take = wanted.get(r.id, (None, None, None, None))
        if (r.chapter_take, r.chapter_index) != take[:2] or (take[0] is not None and r.chapter_offset_s != take[2]):
            updates.append((r.id, *take))
    if updates:
        project.set_chapters(updates)
    return len(updates)
